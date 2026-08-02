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
    lines = agent.logs(which="router", tail=10)["lines"]
    assert lines == ["0.00.193.237 I srv listening on http://127.0.0.1:5001"]


def test_log_tail_reads_a_bounded_window_of_a_huge_file(agent, tmp_path, monkeypatch):
    """A log that grew overnight must cost a fixed read, not a full slurp — the
    tail seeks from the end. The partial line the seek lands mid-way through is
    dropped rather than shown truncated."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    (tmp_path / "router.log").write_text(
        "".join(f"line {i:06d} {'x' * 300}\n" for i in range(20_000)), encoding="utf-8"
    )
    result = agent.logs(which="router", tail=5)
    assert len(result["lines"]) == 5
    assert result["lines"][-1].startswith("line 019999")
    assert all(not line.startswith("ine") for line in result["lines"])


def test_unknown_log_is_404(agent):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        agent.logs(which="../../etc/passwd")
    assert exc.value.status_code == 404


def test_missing_log_reports_absence_rather_than_failing(agent, tmp_path, monkeypatch):
    """Expected before the first start after the launcher gained --log-file."""
    monkeypatch.setattr(agent, "LOG_DIR", tmp_path)
    assert agent.logs(which="router") == {
        "log": "router",
        "path": str(tmp_path / "router.log"),
        "exists": False,
        "lines": [],
    }


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
