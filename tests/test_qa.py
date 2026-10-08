"""QA stage: the section-addressed editing tools (what they let the model change,
and everything they refuse), the verdict schema, and screenshot stripping in the
observability layer."""

import json

import pytest

import episteme.llm.agent as agent
from episteme.config import settings
from episteme.llm import tools
from episteme.llm.observe import _hash_messages, _strip_images
from episteme.llm.schemas import QAReview
from episteme.models import Post
from episteme.worker.qa import (
    _Editor,
    _flush,
    _Renderer,
    needs_render,
    qa_harness,
)

_QUIZ = {
    "type": "quiz",
    "questions": [{"question": "q?", "choices": ["a", "b"], "answer_index": 0, "explanation": "e"}],
}

_SECTIONS = [
    {"type": "prose", "text": "old body"},
    {"type": "key_points", "items": ["a", "b"]},
    _QUIZ,
    {"type": "sources", "items": [{"title": "t", "url": "u", "outlet": "o"}]},
    {"type": "further_reading", "items": [{"title": "f", "url": "v", "outlet": "w"}]},
]


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _editor(candidates=None, sections=None, *, session=None, renderer=None, visual=False):
    """An editor with the harness that would be driving it in a real review.

    The harness rides along on the editor purely so these tests can stay written
    about the editor: it is the same object `harness.context.editor` is, and the
    handlers only ever see it through the context.
    """
    post = Post(
        id=1,
        kind="article",
        title="T",
        summary="S",
        difficulty="intermediate",
        topics=["astronomy"],
        sections=sections or [dict(s) for s in _SECTIONS],
    )
    editor = _Editor.of(post, candidates or {})
    editor.harness = qa_harness(
        editor,
        session or FakeSession(),
        renderer or FakeRenderer(),
        visual=visual,
        on_idle=lambda _text: None,
    )
    return editor


class FakeRenderer:
    """Stands in for headless Chromium; records which posts it was asked to draw."""

    def __init__(self):
        self.shots = []

    async def screenshot(self, post_id):
        self.shots.append(post_id)
        return b"PNG"


async def _call(editor, name, args):
    """One tool call, through the path a real run takes: `agent._dispatch` looks
    the tool up in the harness and hands it the context. A test that called a
    handler directly would keep passing after the loop stopped offering it."""
    return await agent._dispatch(editor.harness, name, args)


def _names(editor) -> list[str]:
    return [tool.name for tool in editor.harness.tools]


# --- what the tools address ------------------------------------------------------


def test_editor_indexes_the_body_only():
    """The DB-built citation tail is not in the index space, so no tool can reach
    it — same principle as the writer never authoring its own sources."""
    editor = _editor()
    assert [s["type"] for s in editor.body] == ["prose", "key_points", "quiz"]
    assert [s["type"] for s in editor.tail] == ["sources", "further_reading"]
    listing = json.loads(editor.listing())
    assert [entry["index"] for entry in listing["sections"]] == [0, 1, 2]
    assert "sources" not in editor.listing()


async def test_replace_section_touches_only_that_section():
    """The whole point of section-addressed editing: a fix to one paragraph must
    leave the quiz — which QA used to have to re-emit blind — byte-identical."""
    editor = _editor()
    reply = await _call(
        editor, "replace_section", {"index": 0, "section": {"type": "prose", "text": "new body"}}
    )
    assert not reply.refused
    assert editor.body[0] == {"type": "prose", "text": "new body"}
    assert editor.body[1] == _SECTIONS[1]
    assert editor.body[2] == _QUIZ
    assert editor.dirty and editor.edits == 1


async def test_every_mutation_returns_the_renumbered_body():
    """An insert shifts every index after it, so the reply re-issues the listing —
    otherwise the model's next call addresses a stale index."""
    editor = _editor()
    reply = await _call(
        editor, "insert_section", {"index": 0, "section": {"type": "prose", "text": "lede"}}
    )
    listing = json.loads(reply.content.split("now:\n", 1)[1])
    assert [e["section"]["type"] for e in listing["sections"]] == [
        "prose",
        "prose",
        "key_points",
        "quiz",
    ]
    assert listing["sections"][3]["index"] == 3


async def test_set_title_rewrites_the_title_and_never_the_summary():
    """QA has no summary tool at all (0050) - the card summary is written from the
    body this review leaves behind, by a stage that runs after it."""
    editor = _editor()
    editor.post.summary = "the card as it stands"
    await _call(editor, "set_title", {"title": "Sharper"})
    assert editor.post.title == "Sharper"
    assert editor.post.summary == "the card as it stands"
    assert (await _call(editor, "set_title", {})).content.startswith("Rejected")


async def test_flush_splices_the_citation_tail_back_on():
    session = FakeSession()
    editor = _editor(session=session)
    await _call(editor, "delete_section", {"index": 1})
    await _flush(session, editor)
    assert [s["type"] for s in editor.post.sections] == [
        "prose",
        "quiz",
        "sources",
        "further_reading",
    ]
    assert session.commits == 1
    assert editor.post.reading_time_minutes >= 1
    await _flush(session, editor)  # nothing pending -> no second write
    assert session.commits == 1


# --- what the tools refuse -------------------------------------------------------


async def test_deleting_the_last_quiz_is_refused():
    """Every post carries a comprehension check. The writer's whole-draft validator
    can't see a single edit, so the invariant is enforced on the action itself."""
    editor = _editor()
    reply = await _call(editor, "delete_section", {"index": 2})
    assert reply.content.startswith("Rejected") and "quiz" in reply.content
    assert [s["type"] for s in editor.body] == ["prose", "key_points", "quiz"]
    assert editor.edits == 0 and not editor.dirty


async def test_replacing_the_last_quiz_with_prose_is_refused():
    editor = _editor()
    reply = await _call(
        editor, "replace_section", {"index": 2, "section": {"type": "prose", "text": "no quiz"}}
    )
    assert reply.content.startswith("Rejected")
    assert editor.body[2] == _QUIZ


async def test_replacing_the_quiz_with_a_corrected_quiz_is_allowed():
    """Fixing a bad question is exactly what the invariant must not block."""
    fixed = {
        "type": "quiz",
        "questions": [
            {"question": "q2?", "choices": ["x", "y"], "answer_index": 1, "explanation": "e2"}
        ],
    }
    editor = _editor()
    await _call(editor, "replace_section", {"index": 2, "section": fixed})
    assert editor.body[2] == fixed


async def test_invalid_section_is_a_tool_error_not_a_failed_review():
    """A malformed section comes back as something the model can repair in the same
    conversation — it used to fail the entire QAReview and burn a repair retry
    re-emitting a whole body that was otherwise correct."""
    editor = _editor()
    reply = await _call(
        editor, "replace_section", {"index": 0, "section": {"type": "carousel", "items": []}}
    )
    assert reply.content.startswith("Rejected") and not reply.refused
    assert editor.body[0] == _SECTIONS[0] and editor.edits == 0

    bad_quiz = {
        "type": "quiz",
        "questions": [
            {"question": "q?", "choices": ["a", "b"], "answer_index": 7, "explanation": "e"}
        ],
    }
    reply = await _call(editor, "replace_section", {"index": 2, "section": bad_quiz})
    assert reply.content.startswith("Rejected")


async def test_media_sections_stay_closed_set():
    """QA can't introduce media the story never ingested; a candidate URL is kept and
    re-stamped with DB attribution (the model never writes attribution itself)."""
    candidates = {
        "https://cdn.example.org/real.jpg": {
            "kind": "image",
            "attribution": "Nature",
            "source_url": "https://nature.com/x",
        }
    }
    editor = _editor(candidates)

    reply = await _call(
        editor,
        "replace_section",
        {
            "index": 0,
            "section": {"type": "image", "url": "https://evil.example/fake.jpg", "caption": "no"},
        },
    )
    assert reply.content.startswith("Rejected")
    assert "https://cdn.example.org/real.jpg" in reply.content  # names what IS allowed
    assert editor.body[0] == _SECTIONS[0]

    await _call(
        editor,
        "replace_section",
        {
            "index": 0,
            "section": {
                "type": "image",
                "url": "https://cdn.example.org/real.jpg",
                "caption": "ok",
            },
        },
    )
    assert editor.body[0]["attribution"] == "Nature"


@pytest.mark.parametrize(
    "name,args",
    [
        ("replace_section", {"index": 9, "section": {"type": "prose", "text": "x"}}),
        ("delete_section", {"index": -1}),
        ("insert_section", {"index": 4, "section": {"type": "prose", "text": "x"}}),
        ("delete_section", {"index": "two"}),
    ],
)
async def test_out_of_range_indices_are_refused(name, args):
    editor = _editor()
    reply = await _call(editor, name, args)
    assert reply.content.startswith("Rejected")
    assert len(editor.body) == 3 and editor.edits == 0


async def test_edit_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "qa_max_edits", 1)
    editor = _editor()
    await _call(
        editor, "replace_section", {"index": 0, "section": {"type": "prose", "text": "one"}}
    )
    reply = await _call(
        editor, "replace_section", {"index": 0, "section": {"type": "prose", "text": "two"}}
    )
    assert reply.refused  # a refusal, so the stuck-loop breaker can end the review
    assert editor.body[0]["text"] == "one"


# --- the loop --------------------------------------------------------------------


def _toolcall(name, args, call_id="c1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args}}],
    }


class ScriptedGateway:
    def __init__(self, script):
        self.script = list(script)
        self.turns = 0

    async def chat_messages(
        self, role, messages, tools=None, response_schema=None, temperature=0.3
    ):
        self.turns += 1
        return self.script.pop(0) if self.script else {"role": "assistant", "content": "done"}


async def test_loop_applies_edits_then_stops_on_finish_review(monkeypatch):
    gw = ScriptedGateway(
        [
            _toolcall(
                "replace_section",
                '{"index": 0, "section": {"type": "prose", "text": "grounded"}}',
                "c1",
            ),
            _toolcall("finish_review", '{"note": "fixed the lede"}', "c2"),
            _toolcall("delete_section", '{"index": 0}', "c3"),  # never reached
        ]
    )
    monkeypatch.setattr(agent, "gateway", gw)
    editor = _editor()
    messages = [{"role": "system", "content": "sys"}]

    await agent.run_tool_loop(editor.harness, messages)

    assert gw.turns == 2  # finish_review ended it; c3 never ran
    assert editor.body[0] == {"type": "prose", "text": "grounded"}
    assert len(editor.body) == 3


async def test_rerender_flushes_first_then_returns_a_fresh_screenshot():
    """The screenshot has to show the edits, so the re-render persists them before
    rendering — the worker screenshots the post through the real web app."""
    session, renderer = FakeSession(), FakeRenderer()
    editor = _editor(session=session, renderer=renderer, visual=True)
    await _call(
        editor, "replace_section", {"index": 0, "section": {"type": "prose", "text": "fixed"}}
    )
    reply = await _call(editor, "rerender", {})

    assert session.commits == 1 and not editor.dirty
    assert editor.post.sections[0]["text"] == "fixed"
    assert renderer.shots == [1]
    # The image rides back as its own user message: a `role: "tool"` result is a
    # plain string and can't carry one.
    assert reply.follow_up["role"] == "user"
    assert reply.follow_up["content"][1]["type"] == "image_url"


async def test_rerender_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "qa_max_screenshots", 0)
    reply = await _call(_editor(visual=True), "rerender", {})
    assert reply.refused


def test_qa_tools_constrain_the_section_argument():
    """Tool arguments are grammar-constrained like a response_format, so `section`
    carries the real Section union — and its `$defs` must sit on the `parameters`
    object, the root the converter resolves `#/$defs/…` against."""
    replace = tools.get("replace_section")
    params = replace.schema["function"]["parameters"]
    assert "QuizSection" in params["$defs"]
    refs = {branch["$ref"] for branch in params["properties"]["section"]["oneOf"]}
    assert "#/$defs/QuizSection" in refs
    # No citation member exists in the union, so a forged sources block can't validate.
    assert not any("Sources" in ref for ref in refs)


def test_qa_has_no_network_tools():
    """QA's grounding set is fixed at write time; a reviewer that can fetch reopens
    the injection surface for no reviewing benefit (decided 2026-08-02)."""
    editing = {"replace_section", "insert_section", "delete_section", "set_title", "finish_review"}
    assert set(_names(_editor())) == editing
    assert set(_names(_editor(visual=True))) == editing | {"rerender"}


# --- when a screenshot is worth taking --------------------------------------------


def test_only_client_rendered_sections_need_a_screenshot():
    """Seven of the nine section types render through a Jinja branch that is a total
    function of their JSON, so the listing already tells the model exactly what the
    reader sees. Chart and diagram store a *program* — a Vega-Lite spec, Mermaid
    source — and the page is whatever the browser makes of it."""
    assert not needs_render(_SECTIONS)
    assert not needs_render([{"type": "image", "url": "u", "caption": "c"}])
    assert not needs_render([{"type": "timeline", "events": []}])
    assert not needs_render(None)
    assert needs_render([*_SECTIONS, {"type": "chart", "spec": {}}])
    assert needs_render([{"type": "diagram", "mermaid": "flowchart LR\n A --> B"}])


def test_rerender_is_offered_only_where_a_render_could_show_something():
    """Leaving it in the table for a text-only post hands the model a multi-second,
    ~1k-image-token way to re-read what its listing says exactly."""
    assert "rerender" not in _names(_editor())
    # ...and it stays the last-but-one tool, ahead of finish_review, so the closing
    # affordance is still what the model sees last.
    assert _names(_editor(visual=True))[-2:] == ["rerender", "finish_review"]


_CHART = {
    "type": "chart",
    "spec": {
        "mark": "bar",
        "data": {"values": [{"city": "Boston", "pct": 16.0}, {"city": "Chicago", "pct": 0.1}]},
        "encoding": {
            "x": {"field": "city", "type": "nominal"},
            "y": {"field": "pct", "type": "quantitative"},
        },
    },
    "caption": "obscuration",
}


async def test_inserting_a_chart_escalates_a_text_only_review_to_visual():
    """`visual` is decided before the loop from what the WRITER left, so a figure the
    editor adds would otherwise be published unseen and scored — never re-reviewed.
    That is the post-427 blank box, reintroduced through the editing path."""
    editor = _editor()
    reply = await _call(editor, "insert_section", {"index": 0, "section": _CHART})

    assert "rerender" in _names(editor)
    # Ahead of the terminal tool, so the closing affordance is still read last.
    assert _names(editor)[-2:] == ["rerender", "finish_review"]
    assert "rerender" in reply.content  # and the model is told, in the tool result
    # The harness grew; the registry it selected from did not.
    assert "rerender" not in _names(_editor())


async def test_editing_prose_does_not_escalate():
    editor = _editor()
    await _call(
        editor, "replace_section", {"index": 0, "section": {"type": "prose", "text": "fixed"}}
    )
    assert "rerender" not in _names(editor)


async def test_escalation_offers_rerender_exactly_once():
    editor = _editor()
    await _call(editor, "insert_section", {"index": 0, "section": _CHART})
    reply = await _call(editor, "insert_section", {"index": 1, "section": _CHART})
    names = _names(editor)
    assert names.count("rerender") == 1
    assert "rerender" not in reply.content


async def test_a_failed_browser_launch_leaves_no_driver_running(monkeypatch):
    """Playwright's driver is a node subprocess started before the browser. Setting
    `_playwright` first meant a launch failure stranded it: the field is overwritten on
    the next post, so `close()` can only ever stop the last one."""
    import sys
    import types

    stopped: list[bool] = []

    class FakeChromium:
        async def launch(self):
            raise RuntimeError("Executable doesn't exist")

    class FakeDriver:
        chromium = FakeChromium()

        async def stop(self):
            stopped.append(True)

    async def _start():
        return FakeDriver()

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: types.SimpleNamespace(start=_start)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)

    renderer = _Renderer()
    for _ in range(2):  # two posts in one batch, both asking for a browser
        with pytest.raises(RuntimeError):
            await renderer._browser_ready()
    assert len(stopped) == 2
    assert renderer._playwright is None and renderer._browser is None


def test_qa_review_schema_rejects_unknown_verdict():
    with pytest.raises(Exception):
        QAReview.model_validate({"verdict": "maybe", "quality_score": 5, "critique": "c"})


# --- observability ---------------------------------------------------------------


def _image_message(payload: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
        ],
    }


def test_strip_images_replaces_payload_and_keeps_text():
    stripped = _strip_images([_image_message("A" * 100_000)])
    parts = stripped[0]["content"]
    assert parts[0] == {"type": "text", "text": "look at this"}
    url = parts[1]["image_url"]["url"]
    assert "stripped" in url and len(url) < 100


def test_strip_images_is_deterministic_for_chain_hashing():
    """The prefix-hash integrity check hashes stripped messages; re-sending the
    same history (with the same inline image) must hash identically."""
    messages = [{"role": "system", "content": "s"}, _image_message("B" * 5000)]
    assert _hash_messages(_strip_images(messages)) == _hash_messages(_strip_images(messages))


def test_strip_images_leaves_plain_messages_untouched():
    messages = [{"role": "user", "content": "plain text"}]
    assert _strip_images(messages) == messages
