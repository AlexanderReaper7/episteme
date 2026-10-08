"""A dead endpoint stops the batch instead of emptying its queue against it (0055).

Every stage loop catches a failed call per item and moves on, which is right for
one bad post and wrong for a server that is not there: on 2026-09-04 `summarize`
put all 36 pending posts through a closed port in 0.9 seconds, returned 0, and
the run was recorded `succeeded`.

The rule these tests hold is one sentence: `LLMUnavailable` is an `LLMError` that
no stage may swallow. The first test is the mechanical half, applied to every
stage in `STAGE_RUNNERS` rather than to the six handlers that exist today.
"""

import ast
import asyncio
import inspect
from types import SimpleNamespace

import httpx
import pytest

from episteme.llm import LLMError, LLMUnavailable
from episteme.llm.gateway import _as_llm_error
from episteme.worker import pipeline
from episteme.worker.pipeline import STAGE_RUNNERS


# --- what counts as gone ------------------------------------------------------------


def test_a_failed_connect_is_unavailable_not_a_failed_call():
    """`httpx.ConnectError` carries the exact text the 09-04 rows carry."""
    error = _as_llm_error(httpx.ConnectError("All connection attempts failed"))
    assert isinstance(error, LLMUnavailable)
    assert "All connection attempts failed" in str(error)


def test_unavailable_is_still_an_llm_error():
    """The contract every degrading caller writes against (0003). Search still
    falls back to literal matches, `_name_clusters` still degrades to member
    names, and a correspondent still files a week it could not embed - none of
    them needed an edit for this change, and this is why."""
    assert issubclass(LLMUnavailable, LLMError)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.HTTPStatusError("400", request=None, response=None),
        ValueError("not json"),
    ],
)
def test_everything_else_stays_an_ordinary_failed_call(exc):
    """A read timeout means the server took the work: the writer's 600s cap
    against a 35B model on a 10GB card is the ordinary case, not an outage. A
    mid-response disconnect is ambiguous - a keep-alive socket closing under a
    healthy server looks identical - so it stays per-item and the next connect
    is what decides."""
    error = _as_llm_error(exc)
    assert isinstance(error, LLMError)
    assert not isinstance(error, LLMUnavailable)


# --- no stage may swallow it --------------------------------------------------------


def _handlers(node: ast.Try) -> list[str]:
    """The exception names one `try` catches, in order, `""` for a bare except."""
    names = []
    for handler in node.handlers:
        target = handler.type
        if target is None:
            names.append("")
        elif isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
        else:  # a tuple or a `|` union of types
            names.append(ast.unparse(target))
    return names


@pytest.mark.parametrize("stage", sorted(STAGE_RUNNERS))
def test_no_stage_swallows_a_dead_endpoint(stage):
    """The property, over the registry rather than over today's handlers.

    A stage that catches `LLMError` or `Exception` around a model call is saying
    "skip this item". That sentence is false when nothing is listening, so every
    such `try` has to name `LLMUnavailable` first. A stage added next year with a
    per-item handler and no re-raise fails here, which is the point: a rule that
    lives only in six places is six places to forget it.
    """
    tree = ast.parse(inspect.getsource(STAGE_RUNNERS[stage]))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught = _handlers(node)
        broad = [name for name in caught if name in ("LLMError", "Exception", "")]
        if not broad:
            continue
        assert "LLMUnavailable" in caught, (
            f"{stage}: a `try` catching {broad[0] or 'everything'} must let "
            "LLMUnavailable through first"
        )
        assert caught.index("LLMUnavailable") < caught.index(broad[0]), (
            f"{stage}: LLMUnavailable is caught after {broad[0]}, so it never fires"
        )


def test_summarize_stops_at_the_first_dead_call(monkeypatch):
    """The behaviour the source check stands for, on the stage that showed it.

    Three posts are due; the loop must raise on the first rather than record two
    more failures and return 0."""
    calls = []

    class _Gateway:
        async def complete_json(self, *_args, **_kwargs):
            calls.append(1)
            raise LLMUnavailable("ConnectError: All connection attempts failed")

    class _Session:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: iter([1, 2, 3]))

        async def get(self, _model, pk):
            return SimpleNamespace(
                id=pk,
                kind="article",
                title="T",
                story_id=7,
                summary=None,
                summarized_at=None,
                sections=[],
            )

        def add(self, _obj):
            pass

        async def commit(self):
            pass

    async def never(_session):
        return False

    monkeypatch.setattr(pipeline, "gateway", _Gateway())
    monkeypatch.setattr(pipeline, "pause_requested", never)

    with pytest.raises(LLMUnavailable):
        asyncio.run(pipeline.summarize_posts(_Session()))
    assert len(calls) == 1


# --- and the run says so ------------------------------------------------------------


def test_a_run_that_loses_the_endpoint_is_not_recorded_as_succeeded():
    """The whole reason this was invisible for six days. `succeeded` with
    `{"summarize": 0}` reads as a quiet night; there is no other place the
    difference could have shown."""
    source = inspect.getsource(pipeline.run_pipeline)
    assert "except LLMUnavailable" in source
    marker = source.index("except LLMUnavailable")
    assert "break" in source[marker : marker + 600], (
        "the run must stop, not start the next stage against the same dead port"
    )
    assert 'run.status = "skipped"' in source
