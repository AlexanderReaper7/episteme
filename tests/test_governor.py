"""Resource governor + host-agent client + the graceful stop composed from them.

`decide` is pure, so the whole policy table is exercised here without a GPU, a
host agent, or a database — the same reason `sweep_stalled_jobs` takes its
manager as an argument.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException

from episteme.config import settings
from episteme.llm.host import HostAgent, HostAgentError
from episteme.worker.control import MANUAL, RESOURCE
from episteme.worker.governor import decide, is_contended

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

QUIET = {"foreign_gpu_percent": 2.0, "vram_free_mb": 9000}
BUSY = {"foreign_gpu_percent": 92.0, "vram_free_mb": 1200, "games_running": ["bf6"]}

POLICY = dict(busy_percent=25.0, min_free_vram_mb=6000, resume_quiet_seconds=300)


def _state(paused, reason=None, since=None, contended_at=None):
    return {
        "paused": paused,
        "reason": reason,
        "since": since,
        # Same fallback `pause_state` applies to rows written before the field
        # existed: unless a test says otherwise, contention was last seen when
        # the pause started.
        "contended_at": contended_at or since,
    }


# --- contention detection ----------------------------------------------------


def test_foreign_load_is_contention_even_while_we_generate():
    """Per-process attribution is the whole point: our own generation pins the
    GPU at ~100%, so a global reading could never distinguish 'we are busy' from
    'someone else is'. `foreign_gpu_percent` already excludes us, which is what
    makes this usable as a pause signal and not only as a start gate."""
    contended, why = is_contended(
        {"foreign_gpu_percent": 92.0, "our_gpu_percent": 99.0, "vram_free_mb": 200},
        our_models_loaded=True,
        **{k: v for k, v in POLICY.items() if k != "resume_quiet_seconds"},
    )
    assert contended
    assert "92%" in why


def test_low_vram_is_ignored_while_we_hold_the_models():
    """VRAM cannot be attributed per process (measured: the Windows counter
    reported 22GB for dwm on a 10GB card). While our own models are resident the
    figure says nothing about contention, so it must not be read as if it did —
    otherwise every loaded model would look like someone else's workload and the
    governor would pause itself in a loop."""
    resources = {"foreign_gpu_percent": 1.0, "vram_free_mb": 300}
    assert not is_contended(
        resources, our_models_loaded=True, busy_percent=25.0, min_free_vram_mb=6000
    )[0]
    # Unloaded, the very same reading IS someone else's and does gate a start.
    contended, why = is_contended(
        resources, our_models_loaded=False, busy_percent=25.0, min_free_vram_mb=6000
    )
    assert contended and "300 MB" in why


def test_missing_measurements_are_not_contention():
    """A probe that returned nothing must not read as 'GPU busy'. Absence of
    evidence stops the pipeline forever; the governor's failure mode is to have
    no opinion."""
    assert not is_contended({}, our_models_loaded=False, busy_percent=25.0, min_free_vram_mb=6000)[0]


# --- decisions ---------------------------------------------------------------


def test_pauses_when_contended_and_running():
    action, why = decide(BUSY, _state(False), now=NOW, our_models_loaded=False, **POLICY)
    assert action == "pause"
    assert "bf6" in why  # the game is named in the reason the panel shows


def test_contention_while_already_paused_holds_rather_than_re_pausing():
    """Not a no-op: the tick still has to record that the GPU is *still* busy,
    which is what `hold` means. The caller re-stamps `contended_at` from it; see
    the test below for what goes wrong when that stamp stops moving."""
    action, why = decide(BUSY, _state(True, RESOURCE, NOW.isoformat()), now=NOW,
                         our_models_loaded=False, **POLICY)
    assert action == "hold"
    assert "already paused" in why


def test_never_lifts_a_manual_pause():
    """The reason field exists for exactly this. A reader who paused by hand
    expects it to hold; a governor that resumed whenever the GPU went quiet would
    silently override them."""
    action, why = decide(QUIET, _state(True, MANUAL, "2026-07-01T00:00:00+00:00"),
                         now=NOW, our_models_loaded=False, **POLICY)
    assert action is None
    assert "not ours to resume" in why


def test_legacy_pause_without_a_reason_is_left_alone():
    """Rows written before `reason` existed read as MANUAL (see control.py), so
    an upgrade cannot hand the governor authority over a pause a human set."""
    action, _ = decide(QUIET, _state(True, MANUAL, None), now=NOW,
                       our_models_loaded=False, **POLICY)
    assert action is None


def test_resume_waits_for_the_quiet_window():
    """Asymmetric by design: yield immediately, return slowly. A lull between two
    loading screens must not trigger a 20GB model load on top of a running game."""
    recent = (NOW - timedelta(seconds=60)).isoformat()
    action, why = decide(QUIET, _state(True, RESOURCE, recent), now=NOW,
                         our_models_loaded=False, **POLICY)
    assert action is None
    assert "60s of 300s" in why

    old = (NOW - timedelta(seconds=600)).isoformat()
    action, _ = decide(QUIET, _state(True, RESOURCE, old), now=NOW,
                       our_models_loaded=False, **POLICY)
    assert action == "resume"


def test_the_quiet_window_measures_quiet_not_the_length_of_the_pause():
    """Regression: the window was anchored to `since`, so a two-hour game meant
    the window had *long* expired and the first momentary dip — a loading screen,
    an alt-tab — resumed straight into it. The window has to restart every time
    contention is observed, which is what `contended_at` records."""
    long_paused = (NOW - timedelta(hours=2)).isoformat()
    still_busy = (NOW - timedelta(seconds=30)).isoformat()

    action, why = decide(
        QUIET,
        _state(True, RESOURCE, long_paused, contended_at=still_busy),
        now=NOW, our_models_loaded=False, **POLICY,
    )
    assert action is None
    assert "30s of 300s" in why

    # Same two-hour pause, but the GPU has actually been quiet since: it lifts.
    quiet_since = (NOW - timedelta(seconds=600)).isoformat()
    action, _ = decide(
        QUIET,
        _state(True, RESOURCE, long_paused, contended_at=quiet_since),
        now=NOW, our_models_loaded=False, **POLICY,
    )
    assert action == "resume"


def test_quiet_and_not_paused_is_a_no_op():
    assert decide(QUIET, _state(False), now=NOW, our_models_loaded=False, **POLICY)[0] is None


def test_resource_pause_without_a_timestamp_can_still_lift():
    """`since` missing must not mean 'wait forever' — that would be a pause with
    no way out."""
    action, _ = decide(QUIET, _state(True, RESOURCE, None), now=NOW,
                       our_models_loaded=False, **POLICY)
    assert action == "resume"


# --- the stored pause flag ---------------------------------------------------


class _FakeSession:
    """Enough of AsyncSession for `pause_state`, which only ever reads one
    scalar. Keeps the safety property below testable without a database."""

    def __init__(self, value):
        self._value = value

    async def execute(self, _statement):
        value = self._value
        return type("R", (), {"scalar": staticmethod(lambda: value)})()


async def test_pause_state_reads_a_legacy_row_as_manual():
    """The rows written before this feature are `{"paused": true}` with no
    author. Defaulting them to MANUAL is what stops an upgrade from silently
    handing the governor permission to resume a pause a human set."""
    from episteme.worker.control import pause_state

    assert await pause_state(_FakeSession({"paused": True})) == {
        "paused": True, "reason": MANUAL, "since": None, "contended_at": None
    }


async def test_contended_at_falls_back_to_since_on_older_rows():
    """A pause written before `contended_at` existed must still be resumable,
    and must not read as "contention observed just now" either — `since` is the
    only evidence such a row carries."""
    from episteme.worker.control import pause_state

    state = await pause_state(
        _FakeSession({"paused": True, "reason": RESOURCE, "since": "2026-07-01T00:00:00+00:00"})
    )
    assert state["contended_at"] == "2026-07-01T00:00:00+00:00"


async def test_pause_state_of_an_unpaused_flag_has_no_author():
    from episteme.worker.control import pause_state

    state = await pause_state(_FakeSession({"paused": False, "reason": RESOURCE}))
    assert state == {"paused": False, "reason": None, "since": None, "contended_at": None}


async def test_pause_state_of_a_missing_row():
    from episteme.worker.control import pause_state

    assert (await pause_state(_FakeSession(None)))["paused"] is False


# --- what a pause is allowed to take away -------------------------------------


def _async(result):
    """A coroutine function returning `result`, evaluated once at patch time."""

    async def _call(*_args, **_kwargs):
        return result

    return _call


class _FakeDB:
    """Stands in for SessionLocal(): an async context manager over one session."""

    def __init__(self, session=None):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


async def test_pause_does_not_unload_a_model_out_from_under_a_running_story(monkeypatch):
    """Regression: the governor unloaded unconditionally, missing the guard
    /api/pipeline/pause has. A game launched mid-write would then rip the model
    out of VRAM during generation — destroying exactly the work unit the gentle
    pause exists to preserve. The worker unloads at its own next boundary."""
    from episteme.worker import governor as gov

    unloads = []

    async def fake_unload():
        unloads.append("unloaded")
        return []

    monkeypatch.setattr(settings, "resource_governor_enabled", True)
    monkeypatch.setattr(settings, "llm_host_agent_url", "http://agent.test")
    monkeypatch.setattr(gov, "SessionLocal", _FakeDB())
    monkeypatch.setattr(gov.host_agent, "resources", _async(BUSY))
    monkeypatch.setattr(gov, "_our_models_loaded", _async(True))
    monkeypatch.setattr(gov, "pause_state", _async(_state(False)))
    monkeypatch.setattr(gov, "set_paused", _async(None))
    monkeypatch.setattr(gov.gateway, "unload_models", fake_unload)

    monkeypatch.setattr(gov, "pipeline_job_running", _async(True))
    assert (await gov.govern_resources())["action"] == "pause"
    assert unloads == []

    # Nothing running: the VRAM a sleeping router still holds is handed back now.
    monkeypatch.setattr(gov, "pipeline_job_running", _async(False))
    assert (await gov.govern_resources())["action"] == "pause"
    assert unloads == ["unloaded"]


def test_the_periodic_is_registered_only_when_the_governor_is_on(monkeypatch):
    """Off must mean off, not "cheap": a `*/2` cron that only ever returns
    "governor disabled" still writes ~720 rows a day into procrastinate_jobs,
    which nothing prunes and which drowns the 20-row admin queue view.

    Split in two because a module-level `if` is only ever evaluated once per
    process: the predicate is checked against every configuration, and the
    registry is checked against the predicate under the one this process was
    imported with."""
    from episteme.worker import governor as gov

    monkeypatch.setattr(settings, "llm_host_agent_url", "http://agent.test")
    monkeypatch.setattr(settings, "resource_governor_enabled", False)
    assert not gov.periodic_enabled()
    monkeypatch.setattr(settings, "resource_governor_enabled", True)
    assert gov.periodic_enabled()
    # Enabled but with no agent to ask is the same nothing: the governor cannot
    # measure anything, so the cron would be pure noise.
    monkeypatch.setattr(settings, "llm_host_agent_url", "")
    assert not gov.periodic_enabled()

    monkeypatch.undo()
    registered = any(
        "govern_resources" in name for name, _id in gov.app.periodic_registry.periodic_tasks
    )
    assert registered == gov.periodic_enabled()


def test_a_loading_model_counts_as_holding_vram():
    """The governor and `unload_models` used to disagree about this — `== "loaded"`
    against `not in (None, "unloaded")`. During the ~100s a model takes to load,
    the governor therefore read our own fresh allocation as someone else's free
    VRAM, and paused + unloaded the model it was in the middle of loading. One
    predicate now answers for both."""
    from episteme.llm.gateway import holds_vram

    assert holds_vram({"id": "m", "status": {"value": "loading"}})
    assert holds_vram({"id": "m", "status": {"value": "loaded"}})
    assert not holds_vram({"id": "m", "status": {"value": "unloaded"}})
    # A plain single-model server reports no status at all: it holds nothing as
    # far as we can tell, and is never sent an unload.
    assert not holds_vram({"id": "m"})


# --- pause bookkeeping -------------------------------------------------------


async def test_marking_contention_does_not_move_the_pause_start():
    """`since` is what the panel shows ("paused since 14:02"); `contended_at` is
    what the resume window measures. Refreshing one must never move the other."""
    from episteme.worker.control import PAUSE_KEY, mark_contended

    class _Session:
        def __init__(self, value):
            self.row = type("Row", (), {"value": value, "key": PAUSE_KEY})()

        async def get(self, _model, _key):
            return self.row

        async def commit(self):
            pass

    started = "2026-01-01T10:00:00+00:00"
    session = _Session({"paused": True, "reason": RESOURCE, "since": started})
    await mark_contended(session)
    assert session.row.value["since"] == started
    assert datetime.fromisoformat(session.row.value["contended_at"]) > datetime.fromisoformat(
        started
    )

    # Nothing paused: there is no window to hold open, so nothing is written.
    unpaused = _Session({"paused": False})
    await mark_contended(unpaused)
    assert unpaused.row.value == {"paused": False}


# --- host agent client -------------------------------------------------------


@pytest.fixture
def agent_url(monkeypatch):
    monkeypatch.setattr(settings, "llm_host_agent_url", "http://agent.test")
    return settings.llm_host_agent_url


def _agent(handler) -> HostAgent:
    agent = HostAgent()
    agent._client = httpx.AsyncClient(
        base_url=settings.llm_host_agent_url.rstrip("/"),
        transport=httpx.MockTransport(handler),
    )
    agent._client_url = settings.llm_host_agent_url.rstrip("/")
    return agent


async def test_reads_degrade_to_none_when_the_agent_is_down(agent_url):
    """The panel must render a dead agent, not raise. Episteme is designed to run
    without one at all."""
    def boom(request):
        raise httpx.ConnectError("refused")

    agent = _agent(boom)
    assert await agent.status() is None
    assert await agent.resources() is None
    assert await agent.logs() is None


async def test_actions_raise_when_the_agent_is_down(agent_url):
    """Opposite convention from reads, deliberately: a start button that silently
    does nothing is worse than one that says why it failed."""
    def boom(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(HostAgentError):
        await _agent(boom).start()


async def test_disabled_agent_is_inert(monkeypatch):
    """The empty URL is the off switch for the entire feature."""
    monkeypatch.setattr(settings, "llm_host_agent_url", "")
    agent = HostAgent()
    assert not agent.enabled
    assert await agent.status() is None
    with pytest.raises(HostAgentError, match="No host agent configured"):
        await agent.start()


async def test_log_tail_defaults_to_the_configured_size(agent_url, monkeypatch):
    monkeypatch.setattr(settings, "llm_log_tail_lines", 42)
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"lines": []})

    await _agent(handler).logs("embed")
    assert seen == {"which": "embed", "tail": "42"}


async def test_reads_and_actions_get_timeouts_sized_for_what_they_wait_on(
    agent_url, monkeypatch
):
    """One timeout for everything was wrong in both directions. Sized for /start
    it made a hung agent block the dashboard — /status rides the page load and
    /logs polls every 3s — for two minutes; sized for a read it would abandon a
    restart that was still legitimately working and report a success as a
    failure, with the processes running."""
    monkeypatch.setattr(settings, "llm_host_agent_read_timeout_seconds", 15.0)
    monkeypatch.setattr(settings, "llm_host_agent_timeout_seconds", 240.0)
    monkeypatch.setattr(settings, "llm_host_agent_restart_timeout_seconds", 360.0)
    seen = {}

    def handler(request):
        seen[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={})

    agent = _agent(handler)
    await agent.status()
    await agent.resources()
    await agent.start()
    await agent.stop()
    await agent.restart()

    assert seen["/status"] == seen["/resources"] == 15.0
    assert seen["/start"] == seen["/stop"] == 240.0
    # A restart is a stop and a start inside one request, so it cannot inherit a
    # ceiling sized for one leg.
    assert seen["/restart"] == 360.0
    assert settings.llm_host_agent_restart_timeout_seconds > (
        settings.llm_host_agent_timeout_seconds
    )


# --- the graceful stop (web/api.py) -------------------------------------------


def _record(entries, name):
    async def _call(_session, *args, **kwargs):
        entries.append((name, args[0] if args else kwargs))

    return _call


async def test_graceful_stop_does_not_pause_when_it_cannot_stop_anything(monkeypatch):
    """Regression: the pause was written first, so an absent or unreachable agent
    503'd *after* leaving the pipeline paused — as MANUAL, which the governor is
    forbidden to lift — with nothing stopped and no llama.cpp problem left to
    explain it. A side effect must not outlive the action it was taken for."""
    from episteme.web import api
    from episteme.worker import control

    entries = []
    monkeypatch.setattr(settings, "llm_host_agent_url", "http://agent.test")
    monkeypatch.setattr(api, "SessionLocal", _FakeDB())
    monkeypatch.setattr(api, "_pipeline_job_running", _async(False))
    monkeypatch.setattr(control, "pause_state", _async(_state(False)))
    monkeypatch.setattr(control, "set_paused", _record(entries, "set_paused"))
    monkeypatch.setattr(control, "restore_pause", _record(entries, "restore_pause"))

    # 1. The agent never answers: nothing is written at all.
    monkeypatch.setattr(api.host_agent, "status", _async(None))
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_backend_stop(force=False)
    assert exc.value.status_code == 503
    assert entries == []

    # 2. It answers /status and then refuses the kill: the flag goes back to the
    #    snapshot taken before, not to a fresh pause nobody asked for.
    async def refuse():
        raise HostAgentError("ConnectError: refused")

    monkeypatch.setattr(api.host_agent, "status", _async({"servers": {}}))
    monkeypatch.setattr(api.host_agent, "stop", refuse)
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_backend_stop(force=False)
    assert exc.value.status_code == 503
    assert [name for name, _ in entries] == ["set_paused", "restore_pause"]
    assert entries[-1][1]["paused"] is False


async def test_graceful_stop_restores_a_pause_it_found_rather_than_clearing_it(monkeypatch):
    """Rolling back means *back*, including whose pause it was and when it
    started. Re-deriving it with set_paused would reset `since` and re-author a
    governor pause as a manual one — which is the one thing that stops the
    governor from ever lifting it again."""
    from episteme.web import api
    from episteme.worker import control

    entries = []
    before = _state(True, RESOURCE, "2026-08-01T10:00:00+00:00")
    monkeypatch.setattr(settings, "llm_host_agent_url", "http://agent.test")
    monkeypatch.setattr(api, "SessionLocal", _FakeDB())
    monkeypatch.setattr(api, "_pipeline_job_running", _async(False))
    monkeypatch.setattr(control, "pause_state", _async(before))
    monkeypatch.setattr(control, "set_paused", _record(entries, "set_paused"))
    monkeypatch.setattr(control, "restore_pause", _record(entries, "restore_pause"))
    monkeypatch.setattr(api.host_agent, "status", _async({"servers": {}}))

    async def refuse():
        raise HostAgentError("ConnectError: refused")

    monkeypatch.setattr(api.host_agent, "stop", refuse)
    with pytest.raises(HTTPException):
        await api.api_llm_backend_stop(force=False)

    assert entries[-1] == ("restore_pause", before)
