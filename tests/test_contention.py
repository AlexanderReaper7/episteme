"""What llama-warden's announcement does here, the client that talks to it, and
the graceful stop composed from both.

The DECISION is not tested here, because it is not made here any more (0057).
`policy.decide` lives in llama-warden with its own table of cases; what this file
covers is the receiving end - which pause may be lifted by whom, what a pause is
allowed to take away, and the idempotency the warden's repeat-until-it-lands
delivery depends on.
"""

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
        "paused": True,
        "reason": MANUAL,
        "since": None,
        "contended_at": None,
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
        "applied": True,
        "paused": True,
        "worker_running": True,
        "unloaded_models": [],
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
    assert [params for _, params in session.calls] == [{"key": LEASE_KEY, "holder": CHAT_HOLDER}]


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


# --- InferMux's warden client (llm/warden.py) ---------------------------------


@pytest.fixture
def agent_url(monkeypatch):
    monkeypatch.setattr(settings, "llm_warden_url", "http://agent.test")
    return settings.llm_warden_url


def _agent(handler, headers=None) -> Warden:
    agent = Warden()
    agent._client = httpx.AsyncClient(
        base_url=settings.llm_warden_url.rstrip("/"),
        headers=headers or {},
        transport=httpx.MockTransport(handler),
    )
    agent._client_url = settings.llm_warden_url.rstrip("/")
    return agent


async def test_reads_degrade_to_none_when_the_warden_is_down(agent_url):
    """The panel must render a dead warden, not raise. Episteme is designed to
    run without one at all."""

    def boom(request):
        raise httpx.ConnectError("refused")

    agent = _agent(boom)
    assert await agent.verdict() is None
    assert await agent.resources() is None
    assert await agent.running() is None


async def test_the_unload_raises_when_the_warden_is_down(agent_url):
    """Opposite convention from reads, deliberately: an unload button that
    silently does nothing is worse than one that says why it failed."""

    def boom(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(WardenError):
        await _agent(boom).unload()


async def test_a_refusal_says_what_infermux_said(agent_url):
    """InferMux puts the reason in `detail`. "409 Conflict" alone would send the
    operator to InferMux's logs for a sentence it already sent us."""

    def refuse(request):
        return httpx.Response(409, json={"detail": "1 interactive request(s) in flight"})

    with pytest.raises(WardenError, match="1 interactive request"):
        await _agent(refuse).unload()


async def test_disabled_warden_is_inert(monkeypatch):
    """The empty URL is the off switch for the entire feature."""
    monkeypatch.setattr(settings, "llm_warden_url", "")
    agent = Warden()
    assert not agent.enabled
    assert await agent.running() is None
    with pytest.raises(WardenError, match="No warden configured"):
        await agent.unload()


async def test_every_request_goes_to_infermux_s_own_paths(agent_url):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    agent = _agent(handler)
    await agent.verdict()
    await agent.resources()
    await agent.running()
    await agent.unload()
    assert seen == [
        ("GET", "/warden/verdict"),
        ("GET", "/warden/resources"),
        ("GET", "/running"),
        ("POST", "/warden/unload"),
    ]


async def test_the_unload_carries_the_header_infermux_requires(agent_url):
    """InferMux refuses a write under /warden/ without X-InferMux, its guard
    against a browser being steered into one (InferMux 0005)."""
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"unloaded": []})

    await _agent(handler).unload()
    assert seen.get("x-infermux")


def test_the_client_sends_this_process_s_key(agent_url, monkeypatch, tmp_path):
    """InferMux answers nothing without a key from keys.yaml. The warden client
    sends the same one the gateway does, so the web asks as `episteme` and the
    worker as `episteme-batch` (0058)."""
    key = tmp_path / "key"
    key.write_text("sk-test\n")
    monkeypatch.setattr(settings, "llm_api_key_file", str(key))
    assert Warden()._http().headers["authorization"] == "Bearer sk-test"


async def test_reads_and_the_unload_get_timeouts_sized_for_what_they_wait_on(
    agent_url, monkeypatch
):
    """Reads are on the dashboard's critical path and must give up in seconds.
    The unload stops every llama-server before it answers, and a ceiling sized
    for a read would report a finished unload as a failure."""
    monkeypatch.setattr(settings, "llm_warden_read_timeout_seconds", 15.0)
    monkeypatch.setattr(settings, "llm_warden_timeout_seconds", 60.0)
    seen = {}

    def handler(request):
        seen[request.url.path] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={})

    agent = _agent(handler)
    await agent.running()
    await agent.resources()
    await agent.unload()

    assert seen["/running"] == seen["/warden/resources"] == 15.0
    assert seen["/warden/unload"] == 60.0


# --- the graceful unload (web/api.py) -----------------------------------------


def _record(entries, name):
    async def _call(_session, *args, **kwargs):
        entries.append((name, args[0] if args else kwargs))

    return _call


async def test_graceful_unload_does_not_pause_when_it_cannot_unload_anything(monkeypatch):
    """Regression: the pause was written first, so an absent or unreachable
    warden 503'd *after* leaving the pipeline paused, as MANUAL, which the warden
    is forbidden to lift, with nothing stopped and no llama.cpp problem left to
    explain it. The rule carried over from llama-warden's stop to InferMux's
    unload. A side effect must not outlive the action it was taken for."""
    from episteme.web import api
    from episteme.worker import control

    entries = []
    monkeypatch.setattr(settings, "llm_warden_url", "http://agent.test")
    monkeypatch.setattr(api, "SessionLocal", _FakeDB())
    monkeypatch.setattr(api, "_pipeline_job_running", _async(False))
    monkeypatch.setattr(control, "pause_state", _async(_state(False)))
    monkeypatch.setattr(control, "set_paused", _record(entries, "set_paused"))
    monkeypatch.setattr(control, "restore_pause", _record(entries, "restore_pause"))

    # 1. InferMux never answers: nothing is written at all.
    monkeypatch.setattr(api.warden, "running", _async(None))
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_backend_unload(force=False)
    assert exc.value.status_code == 503
    assert entries == []

    # 2. It answers /running and then refuses the unload: the flag goes back to
    #    the snapshot taken before, not to a fresh pause nobody asked for.
    async def refuse():
        raise WardenError("1 interactive request(s) in flight")

    monkeypatch.setattr(api.warden, "running", _async({"running": []}))
    monkeypatch.setattr(api.warden, "unload", refuse)
    with pytest.raises(HTTPException) as exc:
        await api.api_llm_backend_unload(force=False)
    assert exc.value.status_code == 503
    assert [name for name, _ in entries] == ["set_paused", "restore_pause"]
    assert entries[-1][1]["paused"] is False


async def test_graceful_unload_restores_a_pause_it_found_rather_than_clearing_it(monkeypatch):
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
    monkeypatch.setattr(api.warden, "running", _async({"running": []}))

    async def refuse():
        raise WardenError("1 interactive request(s) in flight")

    monkeypatch.setattr(api.warden, "unload", refuse)
    with pytest.raises(HTTPException):
        await api.api_llm_backend_unload(force=False)

    assert entries[-1] == ("restore_pause", before)
