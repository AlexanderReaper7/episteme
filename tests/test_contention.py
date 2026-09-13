"""What llama-warden's announcement does here, the client that talks to it, and
the graceful stop composed from both.

The DECISION is not tested here, because it is not made here any more (0057).
`policy.decide` lives in llama-warden with its own table of cases; what this file
covers is the receiving end - which pause may be lifted by whom, what a pause is
allowed to take away, and the idempotency the warden's repeat-until-it-lands
delivery depends on.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException

from episteme.config import settings
from episteme.llm.warden import Warden, WardenError
from episteme.worker.contention import apply_announcement
from episteme.worker.control import MANUAL, RESOURCE

BUSY_REASON = "foreign GPU load 91% >= 25% (bf6)"
QUIET_REASON = "GPU is free"


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
    handing llama-warden permission to resume a pause a human set."""
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
    """Regression, carried over from the governor: it unloaded unconditionally,
    missing the guard /api/pipeline/pause has. A game launched mid-write would
    then rip the model out of VRAM during generation - destroying exactly the
    work unit the gentle pause exists to preserve. The worker unloads at its own
    next boundary instead."""
    from episteme.worker import contention

    unloads = []

    async def fake_unload(_session):
        unloads.append("unloaded")
        return []

    monkeypatch.setattr(contention, "pause_state", _async(_state(False)))
    monkeypatch.setattr(contention, "set_paused", _async(None))
    monkeypatch.setattr(contention, "unload_unless_interactive", fake_unload)

    monkeypatch.setattr(contention, "pipeline_job_running", _async(True))
    result = await apply_announcement(_FakeSession({}), "pause", BUSY_REASON)
    assert result == {
        "applied": True, "paused": True, "worker_running": True, "unloaded_models": []
    }
    assert unloads == []

    # Nothing running: the VRAM a sleeping router still holds is handed back now.
    monkeypatch.setattr(contention, "pipeline_job_running", _async(False))
    assert (await apply_announcement(_FakeSession({}), "pause", BUSY_REASON))["applied"]
    assert unloads == ["unloaded"]


async def test_the_unload_goes_through_the_interactive_lease():
    """The half `pipeline_job_running` cannot see. It counts procrastinate jobs,
    and a chat turn is not a job - it runs in the web process. So the unload must
    route through `unload_unless_interactive`, never `gateway.unload_models`, or
    a warden pause destroys a stream the reader is watching.

    Structural rather than behavioural: what is checked is WHICH function the
    applier calls, because the guard being one call away is the whole property.
    A test that stubbed the lease would pass just as well against a direct
    unload. It reads the PARSED module rather than its text, because the
    docstring that names the function we must not call is not a call to it."""
    import ast
    import inspect

    from episteme.worker import contention

    tree = ast.parse(inspect.getsource(contention))
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert "unload_unless_interactive" in called
    assert "unload_models" not in called


async def test_a_repeated_pause_does_not_move_when_the_pause_began(monkeypatch):
    """The warden re-sends its verdict every 300s until it changes, because it
    pushes rather than leasing - repetition is its only retry. So applying
    `pause` to an already-paused pipeline must re-stamp the freshness clock and
    nothing else. Moving `since` would turn "paused at 18:04" into "paused just
    now" every time anybody looked."""
    from episteme.worker import contention

    marked = []
    monkeypatch.setattr(
        contention, "pause_state", _async(_state(True, RESOURCE, "2026-09-13T18:04:11+00:00"))
    )
    monkeypatch.setattr(contention, "mark_contended", _async(None))
    monkeypatch.setattr(contention, "set_paused", _async("SHOULD NOT BE CALLED"))
    monkeypatch.setattr(
        contention, "mark_contended", lambda session: marked.append(session) or _noop()
    )

    result = await apply_announcement(_FakeSession({}), "pause", BUSY_REASON)
    assert result["applied"] is False
    assert result["paused"] is True
    assert len(marked) == 1


async def test_a_pause_announcement_never_re_authors_a_hand_set_pause(monkeypatch):
    """A human paused; the warden then sees a game and says "pause". The pipeline
    is already stopped, so there is nothing to do - and crucially the author must
    stay MANUAL. Promoting it to RESOURCE would hand the warden permission to
    lift it when the game ends, which is the one thing a hand-set pause is for."""
    from episteme.worker import contention

    monkeypatch.setattr(contention, "pause_state", _async(_state(True, MANUAL, "t")))
    monkeypatch.setattr(contention, "mark_contended", _async(None))
    monkeypatch.setattr(contention, "set_paused", _async("SHOULD NOT BE CALLED"))

    result = await apply_announcement(_FakeSession({}), "pause", BUSY_REASON)
    assert result["applied"] is False
    assert "manual" in result["detail"]


async def test_only_a_resource_pause_may_be_lifted(monkeypatch):
    """Survives from the governor unchanged, because it was never about who owned
    the threshold. The warden cannot tell its own pause from a human's, so the
    stored author is the only thing that can."""
    from episteme.worker import contention

    cleared = []
    monkeypatch.setattr(contention, "set_paused", lambda s, v: cleared.append(v) or _noop())

    monkeypatch.setattr(contention, "pause_state", _async(_state(True, MANUAL, "t")))
    result = await apply_announcement(_FakeSession({}), "resume", QUIET_REASON)
    assert result["applied"] is False
    assert result["paused"] is True
    assert cleared == []

    # A legacy row carries no author at all and reads as MANUAL, same answer.
    monkeypatch.setattr(contention, "pause_state", _async(_state(True, MANUAL, None)))
    assert (await apply_announcement(_FakeSession({}), "resume", QUIET_REASON))["applied"] is False
    assert cleared == []


async def test_resuming_what_is_not_paused_is_a_no_op(monkeypatch):
    """The first tick after the warden restarts re-announces its verdict to every
    consumer, which for a quiet GPU is `resume`. Arriving at a pipeline that was
    never paused, that must do nothing rather than defer a run."""
    from episteme.worker import contention

    monkeypatch.setattr(contention, "pause_state", _async(_state(False)))
    monkeypatch.setattr(contention, "set_paused", _async("SHOULD NOT BE CALLED"))

    result = await apply_announcement(_FakeSession({}), "resume", QUIET_REASON)
    assert result == {"applied": False, "paused": False, "detail": "not paused"}


async def test_an_unknown_action_is_refused_rather_than_guessed(monkeypatch):
    """The endpoint turns this into a 422. A warden that sent something we do not
    understand must be told so, not silently interpreted as one of the two we do
    - and "not pause" defaulting to "resume" would resume into a running game."""
    from episteme.worker import contention

    monkeypatch.setattr(contention, "pause_state", _async(_state(False)))
    with pytest.raises(ValueError, match="yield"):
        await apply_announcement(_FakeSession({}), "yield", "who knows")


async def test_the_endpoint_reports_an_unknown_action_as_a_bad_message(monkeypatch):
    """422, not 500. The warden retries by re-sending its verdict on every tick,
    so a status that reads "Episteme is broken, try again" would have it sending
    the same word it cannot use every 300 s forever. 422 names the message."""
    from episteme.web import api
    from episteme.worker import contention

    monkeypatch.setattr(contention, "pause_state", _async(_state(False)))
    monkeypatch.setattr(api, "SessionLocal", _FakeDB(_FakeSession({})))

    with pytest.raises(HTTPException) as raised:
        await api.api_pipeline_announce({"action": "yield", "reason": "who knows"})
    assert raised.value.status_code == 422
    assert "yield" in raised.value.detail


async def test_only_the_action_is_required_of_an_announcement(monkeypatch):
    """The rest of the body is the warden explaining itself. Refusing a message
    over a missing `reason` would leave the pipeline running through a game to
    punish a formatting mistake, so the reason gets a stand-in and the action is
    read past whatever casing and whitespace it arrived in."""
    from episteme.web import api
    from episteme.worker import contention

    seen = []

    async def record(_session, action, reason):
        seen.append((action, reason))
        return {"applied": True}

    monkeypatch.setattr(contention, "apply_announcement", record)
    monkeypatch.setattr(api, "SessionLocal", _FakeDB(_FakeSession({})))

    assert await api.api_pipeline_announce({"action": "  PAUSE  "}) == {"applied": True}
    assert seen == [("pause", "no reason given")]


def _noop():
    """A completed awaitable, for monkeypatching a coroutine function whose call
    is being recorded rather than replaced."""

    async def _inner():
        return None

    return _inner()


def _lease(**holders) -> dict:
    """The stored lease, from `{holder: seconds from now}`."""
    return {
        "holders": {
            name: (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()
            for name, offset in holders.items()
        }
    }


async def test_an_expired_lease_holds_nothing(monkeypatch):
    """A TTL rather than a lock, because the holder can die. A web process killed
    mid-turn must not strand 20GB of VRAM until someone notices."""
    from episteme.worker.control import interactive_held

    assert await interactive_held(_FakeSession(_lease(chat=-1))) is False
    assert await interactive_held(_FakeSession(_lease(chat=60))) is True
    assert await interactive_held(_FakeSession({})) is False
    assert await interactive_held(_FakeSession({"holders": {}})) is False
    assert await interactive_held(_FakeSession({"holders": {"chat": "nope"}})) is False


async def test_one_holder_leaving_does_not_hand_back_anothers_claim():
    """The lease was a single shared expiry, so whichever deliberate GPU user
    finished first released it for both. A chat turn ending mid-benchmark cleared
    the benchmark's lease, and the governor was then free to evict the model in
    the middle of a measurement - the refresher only re-arms every 100 s."""
    from episteme.worker.control import BENCH_HOLDER, CHAT_HOLDER, interactive_held

    both = _lease(**{CHAT_HOLDER: -1, BENCH_HOLDER: 3600})
    assert await interactive_held(_FakeSession(both)) is True
    # And the converse: an hour-old benchmark entry must not keep the card for a
    # chat turn that is over.
    stale = _lease(**{CHAT_HOLDER: -1, BENCH_HOLDER: -1})
    assert await interactive_held(_FakeSession(stale)) is False


class _Recorder:
    """Captures the statement and its parameters without a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((statement, dict(params or {})))

    async def commit(self):
        pass


async def test_a_release_names_exactly_one_holder():
    """The SQL is what actually scopes it (`- :holder` on the holders object), and
    a fake session cannot execute jsonb. What is pinned here is the parameter that
    reaches it: a release that bound no holder, or the wrong one, is the original
    bug wearing new syntax. The statements themselves were run against the compose
    database on 2026-08-16, both directions plus the legacy upgrade."""
    from episteme.worker.control import CHAT_HOLDER, LEASE_KEY, release_interactive

    session = _Recorder()
    await release_interactive(session, CHAT_HOLDER)
    assert [params for _, params in session.calls] == [
        {"key": LEASE_KEY, "holder": CHAT_HOLDER}
    ]


async def test_the_lease_statements_bind_the_parameters_they_read_as():
    """These two writes are raw SQL because they have to be atomic - the holders
    live in different processes, so a Python-side merge under READ COMMITTED loses
    whichever commits second. Raw SQL means the parameters are parsed out of a
    string, and `text()` scans for `:name` with a negative lookahead on `:`: a
    postfix cast swallows the parameter, so `to_jsonb(:until::text)` binds `unti`
    and fails at execution rather than at import. Written, and caught by this.

    Mechanical rather than a list of expected names: what has to hold is that
    every parameter supplied is one the statement actually asks for, and vice
    versa, whatever they end up being called."""
    from episteme.worker.control import (
        CHAT_HOLDER,
        hold_interactive,
        release_interactive,
    )

    session = _Recorder()
    await hold_interactive(session, CHAT_HOLDER, seconds=60)
    await release_interactive(session, CHAT_HOLDER)
    assert len(session.calls) == 2
    for statement, params in session.calls:
        assert set(statement._bindparams) == set(params)


async def test_a_legacy_single_expiry_lease_is_still_honoured():
    """An upgrade landing between two turns of a live conversation must not evict
    it. The next `hold_interactive` rewrites the value into the holders form."""
    from episteme.worker.control import interactive_held

    live = {"until": (datetime.now(UTC) + timedelta(seconds=60)).isoformat()}
    assert await interactive_held(_FakeSession(live)) is True
    stale = {"until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    assert await interactive_held(_FakeSession(stale)) is False
    assert await interactive_held(_FakeSession({"until": "not a timestamp"})) is False


def test_a_loading_model_counts_as_holding_vram():
    """A model halfway into VRAM occupies it just as much as a finished one.

    This came from two predicates disagreeing, `== "loaded"` against
    `not in (None, "unloaded")`: for the ~100s a load takes, the governor read
    our own fresh allocation as somebody else's free VRAM and unloaded the model
    it was in the middle of loading. The governor left with llama-warden (0057)
    and makes that judgement over there now, against its own sensors. What is
    checked here is the half Episteme kept, `unload_models` handing back a
    loading model rather than walking past it."""
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
    the freshness clock, re-stamped by every re-announcement, and the only thing
    that makes a warden that died look different from a GPU still busy (0057).
    Refreshing one must never move the other."""
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


# --- llama-warden client -----------------------------------------------------


@pytest.fixture
def agent_url(monkeypatch):
    monkeypatch.setattr(settings, "llm_warden_url", "http://agent.test")
    return settings.llm_warden_url


def _agent(handler) -> Warden:
    agent = Warden()
    agent._client = httpx.AsyncClient(
        base_url=settings.llm_warden_url.rstrip("/"),
        transport=httpx.MockTransport(handler),
    )
    agent._client_url = settings.llm_warden_url.rstrip("/")
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

    with pytest.raises(WardenError):
        await _agent(boom).start()


async def test_disabled_agent_is_inert(monkeypatch):
    """The empty URL is the off switch for the entire feature."""
    monkeypatch.setattr(settings, "llm_warden_url", "")
    agent = Warden()
    assert not agent.enabled
    assert await agent.status() is None
    with pytest.raises(WardenError, match="No warden configured"):
        await agent.start()


async def test_log_tail_defaults_to_the_configured_size(agent_url, monkeypatch):
    monkeypatch.setattr(settings, "llm_log_tail_lines", 42)
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"lines": []})

    await _agent(handler).logs("embed")
    # No `since` on a tail read: the parameter is what distinguishes "give me the
    # last N lines" from "give me what came after byte N", so it must be absent
    # rather than sent as some sentinel the agent has to interpret.
    assert seen == {"which": "embed", "tail": "42"}


async def test_a_delta_read_forwards_the_offset(agent_url):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"lines": [], "next_offset": 900})

    await _agent(handler).logs("router", since=900)
    assert seen["since"] == "900"


async def test_reads_and_actions_get_timeouts_sized_for_what_they_wait_on(
    agent_url, monkeypatch
):
    """One timeout for everything was wrong in both directions. Sized for /start
    it made a hung agent block the dashboard — /status rides the page load and
    /logs polls every 3s — for two minutes; sized for a read it would abandon a
    restart that was still legitimately working and report a success as a
    failure, with the processes running."""
    monkeypatch.setattr(settings, "llm_warden_read_timeout_seconds", 15.0)
    monkeypatch.setattr(settings, "llm_warden_timeout_seconds", 240.0)
    monkeypatch.setattr(settings, "llm_warden_restart_timeout_seconds", 360.0)
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
    assert settings.llm_warden_restart_timeout_seconds > (
        settings.llm_warden_timeout_seconds
    )


# --- the graceful stop (web/api.py) -------------------------------------------


def _record(entries, name):
    async def _call(_session, *args, **kwargs):
        entries.append((name, args[0] if args else kwargs))

    return _call


async def test_graceful_stop_does_not_pause_when_it_cannot_stop_anything(monkeypatch):
    """Regression: the pause was written first, so an absent or unreachable
    warden 503'd *after* leaving the pipeline paused, as MANUAL, which the warden
    is forbidden to lift, with nothing stopped and no llama.cpp problem left to
    explain it. A side effect must not outlive the action it was taken for."""
    from episteme.web import api
    from episteme.worker import control

    entries = []
    monkeypatch.setattr(settings, "llm_warden_url", "http://agent.test")
    monkeypatch.setattr(api, "SessionLocal", _FakeDB())
    monkeypatch.setattr(api, "_pipeline_job_running", _async(False))
    monkeypatch.setattr(control, "pause_state", _async(_state(False)))
    monkeypatch.setattr(control, "set_paused", _record(entries, "set_paused"))
    monkeypatch.setattr(control, "restore_pause", _record(entries, "restore_pause"))

    # 1. The agent never answers: nothing is written at all.
    monkeypatch.setattr(api.warden, "status", _async(None))
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_backend_stop(force=False)
    assert exc.value.status_code == 503
    assert entries == []

    # 2. It answers /status and then refuses the kill: the flag goes back to the
    #    snapshot taken before, not to a fresh pause nobody asked for.
    async def refuse():
        raise WardenError("ConnectError: refused")

    monkeypatch.setattr(api.warden, "status", _async({"servers": {}}))
    monkeypatch.setattr(api.warden, "stop", refuse)
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_backend_stop(force=False)
    assert exc.value.status_code == 503
    assert [name for name, _ in entries] == ["set_paused", "restore_pause"]
    assert entries[-1][1]["paused"] is False


async def test_graceful_stop_restores_a_pause_it_found_rather_than_clearing_it(monkeypatch):
    """Rolling back means *back*, including whose pause it was and when it
    started. Re-deriving it with set_paused would reset `since` and re-author a
    RESOURCE pause as a manual one, which is the one thing that stops the warden
    from ever lifting it again."""
    from episteme.web import api
    from episteme.worker import control

    entries = []
    before = _state(True, RESOURCE, "2026-08-01T10:00:00+00:00")
    monkeypatch.setattr(settings, "llm_warden_url", "http://agent.test")
    monkeypatch.setattr(api, "SessionLocal", _FakeDB())
    monkeypatch.setattr(api, "_pipeline_job_running", _async(False))
    monkeypatch.setattr(control, "pause_state", _async(before))
    monkeypatch.setattr(control, "set_paused", _record(entries, "set_paused"))
    monkeypatch.setattr(control, "restore_pause", _record(entries, "restore_pause"))
    monkeypatch.setattr(api.warden, "status", _async({"servers": {}}))

    async def refuse():
        raise WardenError("ConnectError: refused")

    monkeypatch.setattr(api.warden, "stop", refuse)
    with pytest.raises(HTTPException):
        await api.api_llm_backend_stop(force=False)

    assert entries[-1] == ("restore_pause", before)


# --- the log stream (web/api.py) ----------------------------------------------
#
# The pane used to re-fetch its entire 300-line tail every 3s over htmx and swap
# it in. These cover what replaced it: a byte offset carried across calls, so the
# browser is sent only what was appended.


class _FakeStreamRequest:
    """Only `is_disconnected` is touched by the handler. Disconnects after N
    polls, which is how these tests terminate an otherwise endless generator."""

    def __init__(self, polls):
        self.polls = polls

    async def is_disconnected(self):
        self.polls -= 1
        return self.polls < 0


async def _drive(monkeypatch, payloads, since=None):
    """Run the stream against a scripted agent and return (events, offsets asked
    for)."""
    from episteme.web import api

    monkeypatch.setattr(settings, "llm_warden_url", "http://agent.test")
    monkeypatch.setattr(settings, "llm_log_stream_interval_seconds", 0)
    asked = []

    async def fake_logs(which, since=None):
        asked.append(since)
        return payloads[min(len(asked) - 1, len(payloads) - 1)]

    monkeypatch.setattr(api.warden, "logs", fake_logs)
    response = await api.api_llm_logs_stream(
        request=_FakeStreamRequest(len(payloads)), which="router", since=since
    )
    chunks = [chunk async for chunk in response.body_iterator]
    events = []
    for chunk in chunks:
        if chunk.startswith(":"):
            continue
        name = chunk.split("\n", 1)[0].removeprefix("event: ")
        events.append((name, json.loads(chunk.split("data: ", 1)[1])))
    return events, asked


async def test_the_stream_sends_a_tail_once_and_then_only_what_was_appended(monkeypatch):
    """The bytes are the small part of it: re-swapping the whole pane also threw
    away the operator's text selection and scrollback, every 3 seconds, while
    they were reading it."""
    events, asked = await _drive(monkeypatch, [
        {"lines": ["a", "b"], "next_offset": 10, "exists": True, "size_bytes": 10},
        {"lines": ["c"], "next_offset": 12, "exists": True, "size_bytes": 12},
        {"lines": [], "next_offset": 12, "exists": True, "size_bytes": 12},
    ])

    assert [name for name, _ in events] == ["reset", "lines"]
    assert events[0][1]["lines"] == ["a", "b"]
    assert events[1][1]["lines"] == ["c"]
    # Each poll resumes where the last one ended; a quiet log emits nothing at all.
    assert asked == [None, 10, 12]


async def test_the_stream_resumes_a_server_rendered_snapshot_without_resending_it(monkeypatch):
    """The admin fragment renders a tail and hands over its `next_offset`, so the
    handover neither re-sends those lines nor blanks the pane to redraw them."""
    events, asked = await _drive(
        monkeypatch,
        [{"lines": ["new"], "next_offset": 90, "exists": True, "size_bytes": 90}],
        since=42,
    )
    assert asked == [42]
    assert [name for name, _ in events] == ["lines"]   # append, not replace


async def test_a_truncated_log_reaches_the_browser_as_a_replace(monkeypatch):
    """The launcher truncates the log on every start. Appending the new run onto
    the old one is exactly the splice the reset flag exists to prevent."""
    events, _ = await _drive(
        monkeypatch,
        [{"lines": ["fresh"], "next_offset": 6, "reset": True, "gap_bytes": 0, "exists": True}],
        since=5000,
    )
    assert events[0][0] == "reset"


async def test_an_unreachable_agent_does_not_close_the_stream(monkeypatch):
    """Watching llama.cpp restart is a reason to have this pane open, so the
    agent going away has to be an event, not the end of the connection — the
    stream must outlive the process it reports on."""
    events, _ = await _drive(monkeypatch, [
        None,
        {"lines": ["back"], "next_offset": 5, "exists": True, "size_bytes": 5},
    ])
    assert [name for name, _ in events] == ["unavailable", "reset"]
    assert "unreachable" in events[0][1]["detail"]


async def test_the_stream_is_refused_outright_when_no_agent_is_configured(monkeypatch):
    """An EventSource retries forever. Against a feature that is switched off,
    that is a reconnect loop with nothing to reconnect to — so the fragment does
    not offer the stream, and the route says so if something asks anyway."""
    from episteme.web import api

    monkeypatch.setattr(settings, "llm_warden_url", "")
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_logs_stream(request=_FakeStreamRequest(1), which="router", since=None)
    assert exc.value.status_code == 503


# --- the log fragment (admin/_backend_log.html) -------------------------------


def _render_log(**ctx):
    from episteme.web.templating import templates

    base = dict(which="router", logs_available=["router", "embed"], agent_enabled=True,
                since=None)
    return templates.env.get_template("admin/_backend_log.html").render(**{**base, **ctx})


def test_the_log_fragment_does_not_poll():
    """Regression: this fragment used to re-fetch and re-swap its entire tail
    every 3s, which discarded the reader's selection and scrollback each tick.
    The stream replaced the poll — it did not join it."""
    html = _render_log(log={"exists": True, "size_bytes": 1024, "lines": ["x"]}, since=40)
    assert "hx-trigger" not in html
    assert "data-log-stream" in html


def test_the_stream_url_resumes_where_the_rendered_snapshot_ended():
    """The handover: without the offset the stream would re-send the lines that
    are already on the page, and the pane would blink on every load."""
    html = _render_log(log={"exists": True, "size_bytes": 1024, "lines": ["x"]}, since=40)
    assert "since=40" in html


def test_an_unreachable_agent_still_gets_a_pane_to_fill():
    """The old poll recovered on its own when the agent came back. A fragment
    that rendered only an error message would need a manual reload to ever show
    anything — worse than what it replaced."""
    html = _render_log(log=None)
    assert "data-log-stream" in html and "since=" not in html


def test_no_stream_is_offered_when_the_agent_is_switched_off():
    """An EventSource retries forever, and `llm_warden_url` empty is the off
    switch for the whole feature — there is nothing to reconnect to."""
    assert "data-log-stream" not in _render_log(agent_enabled=False, log=None)


async def test_an_agent_predating_the_offset_protocol_degrades_to_replacing(monkeypatch):
    """The agent runs on the host and this runs in a container: they are deployed
    separately, so an agent that answers without `next_offset` is a normal state,
    not a bug. Appending its answers would re-append the whole tail every second;
    treating them as replaces is exactly the poll this stream came from."""
    events, asked = await _drive(monkeypatch, [
        {"lines": ["a", "b"], "exists": True, "size_bytes": 10},
        {"lines": ["a", "b"], "exists": True, "size_bytes": 10},
    ], since=None)

    assert [name for name, _ in events] == ["reset", "reset"]
    assert asked == [None, None]  # no offset to advance to, so none is claimed


def test_a_snapshot_without_an_offset_omits_the_parameter_entirely():
    """Regression, observed live as a 422 the browser then retried forever: an
    empty `?since=` is not the same as no `since`, and FastAPI rejects it. The
    old-agent case is exactly where the value goes missing."""
    html = _render_log(log={"exists": True, "size_bytes": 8, "lines": ["x"]}, since=None)
    assert "since" not in html
