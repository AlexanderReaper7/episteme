"""The llama.cpp panel on /admin (templates/admin/_backend*.html).

Both claims here are about a panel that told the operator nothing while doing
something:

- a lifecycle click can be unanswered for a minute (the agent holds /start until
  both ports answer), and the button has to say so for that whole minute;
- the panel used to be a snapshot taken at page load, so a backend that went down
  on its own - or came up from the host - left it asserting the opposite until
  somebody pressed F5.

The digest tests are the mechanical part: "polls" is worth nothing if the digest
never moves, and worth negative if it moves on its own.
"""

import asyncio
import re

from starlette.requests import Request

from episteme.web.admin import backend_context, backend_partial
from episteme.web.templating import templates

UP = {
    "enabled": True,
    "url": "http://host.docker.internal:5003",
    "status": {
        "servers": {
            "router": {"port": 5001, "listening": True, "pid": 4242, "started_at": None},
            "embed": {"port": 5002, "listening": True, "pid": 4243, "started_at": None},
        }
    },
}
RUNNING = {"paused": False, "reason": None, "since": None, "contended_at": None}
LLM = {
    "endpoints": [
        {
            "url": "http://host.docker.internal:5001",
            "roles": ["main", "fast", "chat"],
            "available": True,
            "models": [{"id": "Qwopus-35B", "status": "loaded"}],
        },
        {
            "url": "http://host.docker.internal:5002",
            "roles": ["embed"],
            "available": True,
            "models": [{"id": "Octen-Embedding-4B.Q8_0", "status": None}],
        },
    ],
    "models": [
        {"id": "Qwopus-35B", "status": "loaded", "endpoint": "http://host.docker.internal:5001"},
        {
            "id": "Octen-Embedding-4B.Q8_0",
            "status": None,
            "endpoint": "http://host.docker.internal:5002",
        },
    ],
    "roles": {"main": {"model": "Qwopus-35B", "url": "http://host.docker.internal:5001"}},
}


def _down(**servers):
    """UP with some servers knocked over. `_down(router=False)` is the state the
    panel could not previously reach without a reload."""
    status = {
        "servers": {
            name: {**row, "listening": servers.get(name, row["listening"])}
            for name, row in UP["status"]["servers"].items()
        }
    }
    return {**UP, "status": status}


def _context(backend=None, pipeline=None, llm=None, **kwargs):
    """backend_context with every read pre-supplied, so nothing touches the host
    agent, the endpoints or the database - the same path the dashboard render
    takes."""
    return asyncio.run(
        backend_context(backend or UP, pipeline or RUNNING, llm or LLM, **kwargs)
    )


def _panel(**kwargs):
    return templates.env.get_template("admin/_backend.html").render(**_context(**kwargs))


# --- the spinner ------------------------------------------------------------


def _buttons(html):
    return re.findall(r"<button[^>]*>.*?</button>", html, re.S)


def test_every_lifecycle_button_carries_a_busy_glyph():
    """`hx-disabled-elt` greys nothing on its own - there is no global :disabled
    rule - so without the swapped glyph a pressed button is indistinguishable
    from one whose request never fired. The pair must both be present: CSS shows
    one and hides the other, it cannot conjure a glyph that is not in the DOM."""
    for button in _buttons(_panel(offer_force=True)):
        assert 'class="icon icon-idle"' in button, button
        assert "icon-busy icon-spin" in button, button


def test_a_click_outranks_the_poll_and_the_poll_never_interrupts_a_click():
    """One sync group over #backend. Without it the 5s tick would swap the very
    button whose request is in flight, taking its spinner with it; and two
    lifecycle actions could race on one pair of processes."""
    panel = _panel(offer_force=True)
    for button in _buttons(panel):
        assert 'hx-sync="closest #backend:replace"' in button, button
    root = re.search(r'<div id="backend-state"[^>]*>', panel).group(0)
    assert 'hx-sync="closest #backend:drop"' in root


# --- the poll ---------------------------------------------------------------


def test_the_polled_block_names_its_own_target():
    """.admin-main sets hx-target="#admin-main" and htmx INHERITS hx-target, so a
    self-replacing fragment that relies on the default swallows the whole
    dashboard and then polls against an element it just deleted. This bit the
    backend log pane once already."""
    root = re.search(r'<div id="backend-state"[^>]*>', _panel()).group(0)
    assert 'hx-target="this"' in root
    assert 'hx-swap="outerHTML"' in root
    assert "every 5s" in root


def test_the_poll_url_carries_the_digest_of_what_is_on_screen():
    context = _context()
    root = re.search(r'<div id="backend-state"[^>]*>', _panel()).group(0)
    assert f"/admin/partials/backend?v={context['backend_hash']}" in root


def test_the_force_offer_rides_in_the_poll_url():
    """It means "a graceful stop was already tried and timed out" - a fact about
    the last action, not about the processes, so the server cannot re-derive it.
    Carried by the fragment, like `v`, or the next tick would delete the button
    the operator was reaching for."""
    root = re.search(r'<div id="backend-state"[^>]*>', _panel(offer_force=True)).group(0)
    assert "offer_force=true" in root
    assert "force stop" in _panel(offer_force=True)
    assert "offer_force" not in re.search(
        r'<div id="backend-state"[^>]*>', _panel()
    ).group(0)


def test_the_action_message_sits_outside_the_polled_block():
    """The whole reason for the split. A message is the answer to a button press;
    rendered by the polled template it would be erased by the next tick, four
    seconds after appearing. Asserted on the template that polls, not on where the
    text lands in the string: the poll replaces #backend-state with whatever
    _backend_state.html renders, so absent there IS the guarantee."""
    context = _context(message="waited 120s, the work unit is still running", failed=True)
    assert "waited 120s" in templates.env.get_template("admin/_backend.html").render(**context)
    polled = templates.env.get_template("admin/_backend_state.html").render(**context)
    assert "waited 120s" not in polled


# --- the endpoint card rides along --------------------------------------------


def _polled(**kwargs):
    return templates.env.get_template("admin/_backend_state.html").render(
        **_context(oob_llm=True, **kwargs)
    )


def test_the_endpoint_card_rides_back_on_the_poll():
    """It has no clock of its own: separately timed, the endpoint card and the
    process card would show "router:5001 listening" beside "endpoint down" for
    seconds at a stretch."""
    polled = _polled()
    assert 'id="llm-endpoints"' in polled
    assert 'hx-swap-oob="true"' in polled
    assert "Qwopus-35B" in polled


def test_the_oob_card_is_top_level_in_the_response():
    """htmx locates an out-of-band element among the fragment's own children.
    Nested inside #backend-state it would be invisible to the swap AND leave a
    second element with that id in the page."""
    polled = _polled()
    assert polled.index('id="llm-endpoints"') > polled.rindex("</div>")


def test_a_full_page_render_gets_a_plain_section():
    """`oob_llm` is set only by the poll route. Marked on the dashboard render it
    would ask htmx to swap an element into the page that IS the page."""
    card = templates.env.get_template("admin/_llm_endpoints.html").render(**_context())
    assert 'id="llm-endpoints"' in card
    assert "hx-swap-oob" not in card
    assert "hx-swap-oob" not in templates.env.get_template("admin/_backend_state.html").render(
        **_context()
    )


# --- the digest: it has to move on state, and only on state -----------------


def test_the_digest_moves_when_a_server_goes_down():
    assert _context()["backend_hash"] != _context(_down(router=False))["backend_hash"]


def test_the_digest_moves_when_the_agent_becomes_unreachable():
    unreachable = {**UP, "status": None}
    assert _context()["backend_hash"] != _context(unreachable)["backend_hash"]


def test_the_digest_moves_when_the_pipeline_is_paused():
    paused = {"paused": True, "reason": "resource", "since": "2026-08-16T09:00:00+00:00"}
    assert _context()["backend_hash"] != _context(UP, paused)["backend_hash"]


def test_the_digest_moves_when_an_endpoint_stops_answering():
    """The user-visible symptom that started this: the endpoint dot stayed green
    until somebody reloaded. If the digest does not carry endpoint health, the
    poll answers 204 forever and the card never comes back."""
    down = {**LLM, "endpoints": [{**LLM["endpoints"][0], "available": False, "models": []},
                                 LLM["endpoints"][1]]}
    assert _context()["backend_hash"] != _context(UP, RUNNING, down)["backend_hash"]


def test_the_digest_moves_when_the_router_swaps_which_model_is_loaded():
    """main↔fast is the state change with no process-level trace at all: same
    PIDs, same ports, same uptime. Only the endpoint inventory says it happened."""
    swapped = {**LLM, "endpoints": [
        {**LLM["endpoints"][0], "models": [{"id": "Qwopus-Fast", "status": "loaded"}]},
        LLM["endpoints"][1],
    ]}
    assert _context()["backend_hash"] != _context(UP, RUNNING, swapped)["backend_hash"]


def _resources_card(busy_percent):
    return templates.env.get_template("admin/_backend_resources.html").render(
        resources={
            "foreign_gpu_percent": 40.0,
            "our_gpu_percent": 0.0,
            "vram_used_mb": 900,
            "vram_total_mb": 10240,
            "vram_free_mb": 9340,
            "games_running": [],
        },
        busy_percent=busy_percent,
    )


def test_the_panel_draws_the_line_the_warden_gave_it():
    """The threshold comes back on `/verdict` and is not a setting here any more
    (0057). 40% foreign load is loud against the warden's 25 and quiet against a
    warden that was told 50, and the panel must agree with whichever one is
    actually deciding rather than with a number of its own."""
    assert "bad" in _resources_card(25.0)
    assert "yields at 25.0%" in _resources_card(25.0)
    assert "bad" not in _resources_card(50.0)


def test_a_warden_that_did_not_say_where_the_line_is_gets_no_line_drawn():
    """`/resources` answered and `/verdict` did not. Colouring against a fallback
    would state a threshold nobody set, and `40.0 >= None` is a TypeError, which
    is a 500 on the panel that exists to show the GPU is fine."""
    card = _resources_card(None)
    assert "40.0%" in card
    assert "yields at" not in card
    assert "bad" not in card


def test_the_digest_ignores_pause_fields_the_panel_does_not_render():
    """`contended_at` is re-stamped by every one of llama-warden's repeat
    announcements and appears nowhere in this panel. Hashing `pipeline` whole
    would re-render the block, and destroy a text selection in it, on the
    warden's clock rather than on a change anybody can see."""
    later = {**RUNNING, "contended_at": "2026-08-16T09:05:00+00:00"}
    assert _context()["backend_hash"] == _context(UP, later)["backend_hash"]


# --- the route --------------------------------------------------------------


def _request():
    return Request(
        {"type": "http", "method": "GET", "path": "/admin/partials/backend",
         "headers": [], "query_string": b""}
    )


def _stub_context(monkeypatch, **kwargs):
    context = _context(**kwargs)

    async def fake(*args, **kw):
        return {**context, "offer_force": kw.get("offer_force", False)}

    monkeypatch.setattr("episteme.web.admin.backend_context", fake)
    return context


def test_an_unchanged_panel_answers_204(monkeypatch):
    """htmx does not swap a 204, so a still backend costs one conditional request
    and NO dom replacement (0033)."""
    context = _stub_context(monkeypatch)
    response = asyncio.run(backend_partial(_request(), v=context["backend_hash"]))
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"


def test_a_moved_panel_answers_the_new_block(monkeypatch):
    _stub_context(monkeypatch, backend=_down(router=False))
    response = asyncio.run(backend_partial(_request(), v="a-stale-digest"))
    assert response.status_code == 200
    body = response.body.decode()
    assert 'id="backend-state"' in body
    assert "not running" in body


def test_a_first_poll_with_no_digest_gets_content(monkeypatch):
    """A hand-written URL, or a fragment rendered before this mechanism existed,
    carries no `v` and must get real content rather than a 204 it cannot read."""
    _stub_context(monkeypatch)
    assert asyncio.run(backend_partial(_request())).status_code == 200
