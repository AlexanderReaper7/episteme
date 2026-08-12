"""Host control agent (`hostagent/llama_agent.py`).

The agent lives outside the package — it runs on the Windows host, not in the
container — so it is imported by path here. Its PowerShell probes are stubbed:
what is worth testing is the logic wrapped around them, which is where the live
defects actually were.
"""

import importlib.util
import threading
import time
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).resolve().parents[1] / "hostagent" / "llama_agent.py"


@pytest.fixture
def agent():
    spec = importlib.util.spec_from_file_location("llama_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ansi_escapes_are_stripped_from_log_lines(agent, tmp_path, monkeypatch):
    """llama-server colors its output and `--log-file` receives the escape codes
    verbatim, so an unfiltered pane renders "\\x1b[34m0.00.193\\x1b[0m" as noise.
    Found live on the first real start."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    (tmp_path / "router.log").write_text(
        "\x1b[34m0.00.193.237\x1b[0m \x1b[32mI\x1b[0m srv listening on http://127.0.0.1:5001\n",
        encoding="utf-8",
    )
    lines = agent.read_log(which="router", tail=10)["lines"]
    assert lines == ["0.00.193.237 I srv listening on http://127.0.0.1:5001"]


def test_log_tail_reads_a_bounded_window_of_a_huge_file(agent, tmp_path, monkeypatch):
    """A log that grew overnight must cost a fixed read, not a full slurp — the
    tail seeks from the end. The partial line the seek lands mid-way through is
    dropped rather than shown truncated."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    (tmp_path / "router.log").write_text(
        "".join(f"line {i:06d} {'x' * 300}\n" for i in range(20_000)), encoding="utf-8"
    )
    result = agent.read_log(which="router", tail=5)
    assert len(result["lines"]) == 5
    assert result["lines"][-1].startswith("line 019999")
    assert all(not line.startswith("ine") for line in result["lines"])


def _write(path, text):
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)


def test_delta_read_returns_only_what_was_appended(agent, tmp_path, monkeypatch):
    """The whole point of the offset protocol: a live pane appends what arrived
    instead of re-fetching a 300-line tail every few seconds, which is both ~99%
    less data and the only way the reader's text selection and scrollback in the
    pane survive an update."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    path = tmp_path / "router.log"
    _write(path, "first\nsecond\n")

    tail = agent.read_log(which="router")
    assert tail["lines"] == ["first", "second"]

    # Nothing new: an empty answer, and the offset does not move.
    idle = agent.read_log(which="router", since=tail["next_offset"])
    assert idle["lines"] == [] and idle["next_offset"] == tail["next_offset"]

    _write(path, "third\n")
    delta = agent.read_log(which="router", since=idle["next_offset"])
    assert delta["lines"] == ["third"]
    assert delta["reset"] is False
    assert delta["next_offset"] == delta["size_bytes"]


def test_a_half_written_line_is_withheld_until_it_is_complete(agent, tmp_path, monkeypatch):
    """The server is writing while we read. Shipping the bytes so far would
    render a truncated line, and — worse — advancing past them would make the
    rest of that line arrive as a line of its own."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    path = tmp_path / "router.log"
    _write(path, "done\n")
    start = agent.read_log(which="router")["next_offset"]

    _write(path, "half a li")
    partial = agent.read_log(which="router", since=start)
    assert partial["lines"] == []
    assert partial["next_offset"] == start  # stayed put, deliberately

    _write(path, "ne\n")
    assert agent.read_log(which="router", since=partial["next_offset"])["lines"] == ["half a line"]


def test_a_truncated_log_tells_the_caller_to_reset(agent, tmp_path, monkeypatch):
    """The launcher truncates the log on every start, so an offset held across a
    restart points into the middle of a different file. Appending onto it would
    splice the new run's output into the old run's, with a mangled first line."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    path = tmp_path / "router.log"
    _write(path, "old run, many lines\n" * 50)
    stale = agent.read_log(which="router")["next_offset"]

    path.write_text("new run\n", encoding="utf-8")
    after = agent.read_log(which="router", since=stale)
    assert after["reset"] is True
    assert after["lines"] == ["new run"]
    assert after["gap_bytes"] == 0  # nothing was skipped, the file simply restarted


def test_a_backlog_past_the_window_resets_and_measures_the_gap(agent, tmp_path, monkeypatch):
    """A delta read is bounded by the same window as a tail read — a pane left
    open through a noisy night must not pull the whole file. The caller is told
    to replace rather than append, and how much it missed, so it can admit the
    discontinuity instead of quietly splicing two distant parts of the log."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    path = tmp_path / "router.log"
    _write(path, "".join(f"line {i:06d} {'x' * 300}\n" for i in range(2_000)))

    result = agent.read_log(which="router", tail=5, since=0)
    assert result["reset"] is True
    assert len(result["lines"]) == 5
    assert result["lines"][-1].startswith("line 001999")
    assert result["gap_bytes"] == result["size_bytes"] - 5 * 400
    # The tail-read rule still holds: no truncated line at the head of the window.
    assert all(line.startswith("line ") for line in result["lines"])


def test_unknown_log_is_404(agent):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        agent.read_log(which="../../etc/passwd")
    assert exc.value.status_code == 404


def test_missing_log_reports_absence_rather_than_failing(agent, tmp_path, monkeypatch):
    """Expected before the first start after the launcher gained --log-file."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    assert agent.read_log(which="router") == {
        "log": "router",
        "path": str(tmp_path / "router.log"),
        "exists": False,
        "lines": [],
        "next_offset": 0,
        "reset": False,
    }
    # A caller holding an offset from before a restart must DROP what it shows,
    # not sit waiting to append onto a file that no longer exists.
    assert agent.read_log(which="router", since=900)["reset"] is True


def test_concurrent_starts_launch_exactly_once(agent, monkeypatch):
    """Regression: a bare "is it listening?" check is a TOCTOU race and loses it.
    Measured live — two requests 300ms apart both saw nothing listening, both ran
    the launcher, and FOUR llama-server processes came up. The launcher's own
    Get-NetTCPConnection guard has the identical hole, so serializing here is the
    only thing that closes it.

    The lock must cover the port wait, not just the check: releasing it after
    spawning would let the next caller observe the not-yet-bound port and launch
    a second server anyway."""
    launches = []
    started = threading.Event()

    def fake_run_ps(script, timeout=30.0):
        if "launch-llama" in script:
            launches.append(script)
            time.sleep(0.2)  # the window the race used to open
            started.set()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(agent, "_run_ps", fake_run_ps)
    # Ports report down until the launcher has run, then up — the real sequence.
    monkeypatch.setattr(agent, "_port_open", lambda port, timeout=0.5: started.is_set())

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(agent.start())) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(launches) == 1, f"launcher ran {len(launches)} times; must be exactly 1"
    assert sum(1 for r in results if r["started"]) == 1
    assert all(r["reason"] == "already running" for r in results if not r["started"])


def test_start_reports_failure_when_a_server_never_binds(agent, monkeypatch):
    """A zero exit code is not evidence that the servers came up. PowerShell's
    default $ErrorActionPreference is Continue, so a launcher that failed on a
    missing model or a bad preset writes its complaint to the console and still
    exits 0 — which used to be reported as `started: true`, sending the operator
    to a working-looking panel while the one thing that explains it (the captured
    output) was thrown away. The ports are the actual claim."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        agent,
        "_run_ps",
        lambda script, timeout=30.0: type(
            "R", (), {"returncode": 0, "stdout": "model file not found: qwopus.gguf", "stderr": ""}
        )(),
    )
    monkeypatch.setattr(agent, "_port_open", lambda port, timeout=0.5: False)
    monkeypatch.setattr(
        agent,
        "_wait_for_ports",
        lambda **kwargs: {"router": {"listening": False}, "embed": {"listening": True}},
    )

    with pytest.raises(HTTPException) as exc:
        agent.start()
    assert exc.value.status_code == 500
    assert "router" in exc.value.detail
    assert "model file not found" in exc.value.detail  # the reason survives


def test_status_skips_the_subprocess_when_nothing_is_listening(agent, monkeypatch):
    """The backend-is-down path is exactly when someone is staring at the admin
    page, so it must not pay for a PowerShell round-trip to describe processes
    that do not exist."""
    calls = []
    monkeypatch.setattr(agent, "_port_open", lambda port, timeout=0.5: False)
    monkeypatch.setattr(agent, "_run_ps", lambda *a, **kw: calls.append(a) or None)

    rows = agent._server_status()
    assert calls == []
    assert set(rows) == {"router", "embed"}
    assert all(row["listening"] is False and row["pid"] is None for row in rows.values())


def test_resources_separates_our_gpu_load_from_everyone_elses(agent, monkeypatch):
    """The governor's whole policy rests on this split: our own generation pins
    the GPU at ~100%, so a figure that included it could never mean 'someone else
    needs the card'."""
    monkeypatch.setattr(
        agent,
        "_run_ps",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stderr": "", "stdout": (
            '{"processes":['
            '{"pid":1,"name":"bf6","percent":90.0,"ours":false},'
            '{"pid":2,"name":"llama-server","percent":45.0,"ours":true},'
            '{"pid":3,"name":"Code","percent":0.5,"ours":false}],'
            '"vram":{"used_mb":9000,"total_mb":10240,"utilization":99},'
            '"running":["bf6","Code"]}'
        )})(),
    )
    monkeypatch.setattr(agent, "_known_games", lambda: {"bf6", "Anno1800"})

    result = agent.resources()
    assert result["foreign_gpu_percent"] == 90.5
    assert result["our_gpu_percent"] == 45.0
    assert result["vram_free_mb"] == 1240
    # Only games actually running, not the whole installed catalogue.
    assert result["games_running"] == ["bf6"]
    # Busiest first, so the panel shows who is using the GPU.
    assert [p["name"] for p in result["processes"]][:2] == ["bf6", "llama-server"]
