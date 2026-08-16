"""The error page (web/errors.py, 0043).

Three claims, and each of them is the reason the page exists:

- an HTML request gets a *page*, with the traceback in it, not `Internal Server
  Error`;
- an `/api` request's JSON body is unchanged, because `detail` is a contract;
- a boosted navigation gets a swappable fragment, because htmx drops a 5xx body
  by default and an unseen error page is the same as no error page.

The routes are added to a throwaway app rather than to the real one: the point
is the handlers, and pinning them to a route that happens to be broken today
would make the test evaporate the moment that route is fixed.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from episteme.web.errors import install_error_handlers

BOOSTED = {"HX-Request": "true", "HX-Target": "main-content"}


def _the_bug(value):
    """A frame with a recognizable name, so the frame list can be identified as
    this stack and not some other one."""
    return value["missing"]


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/boom")
    @app.get("/api/boom")
    async def boom():
        return _the_bug({})

    @app.get("/gone")
    @app.get("/api/gone")
    async def gone():
        raise HTTPException(404, "no such thing")

    @app.get("/typed")
    async def typed(limit: int = 0):
        return {"limit": limit}

    install_error_handlers(app)
    # The real server re-raises after the handler runs, so uvicorn logs it; the
    # test client would turn that into a test failure instead of a response.
    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_renders_a_page_with_the_traceback(client):
    response = client.get("/boom")
    assert response.status_code == 500
    body = response.text
    assert "<!DOCTYPE html>" in body or "<html" in body
    assert "Internal Server Error" in body
    assert "KeyError" in body
    assert "_the_bug" in body            # the frame list is this stack
    assert "return value[" in body       # ...with source lines (Jinja escapes the quotes)
    assert "Raw traceback" in body
    # The error page must not be the fallback: reaching the fallback means the
    # template failed, and the two are otherwise easy to confuse on sight.
    assert "The error page itself failed to render" not in body


def test_the_page_names_the_request_that_failed(client):
    body = client.get("/boom?why=this").text
    assert "/boom" in body
    assert "why=this" in body


def test_api_keeps_its_json_body(client):
    response = client.get("/api/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


def test_api_http_exception_detail_is_untouched(client):
    response = client.get("/api/gone")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such thing"}


def test_http_exception_on_an_html_route_renders_the_page(client):
    response = client.get("/gone")
    assert response.status_code == 404
    assert "Not Found" in response.text
    assert "no such thing" in response.text


def test_a_bare_status_does_not_print_its_own_phrase_twice(client):
    """`HTTPException(404)` carries "Not Found" as its detail, which is the
    heading again. The summary line exists to say something the status did not."""
    app = client.app

    @app.get("/bare")
    async def bare():
        raise HTTPException(404)

    body = client.get("/bare").text
    assert "Not Found" in body                 # the heading still says it
    assert 'class="error-summary"' not in body  # ...and nothing repeats it
    # The line is present when the detail is not just the status phrase.
    assert 'class="error-summary"' in client.get("/gone").text


def test_validation_error_on_an_html_route_names_the_field(client):
    response = client.get("/typed?limit=nonsense")
    assert response.status_code == 422
    assert "limit" in response.text
    assert "<html" in response.text


def test_boosted_navigation_gets_a_swappable_fragment(client):
    response = client.get("/boom", headers=BOOSTED)
    assert response.status_code == 500
    # The header app.js keys on; without it htmx discards the body and the reader
    # sees a click that did nothing.
    assert response.headers["HX-Error-Page"] == "true"
    assert response.headers["HX-Retarget"] == "#main-content"
    body = response.text
    assert "<html" not in body and "<body" not in body   # no document chrome
    assert "<title>Episteme • 500 Internal Server Error</title>" in body
    assert "_the_bug" in body


def test_a_polled_fragment_failing_is_not_swappable(client):
    """A background poll must never blow away the page being read, so its failure
    carries no swap header even though it gets the same rendered page."""
    response = client.get("/boom", headers={"HX-Request": "true", "HX-Target": "queue"})
    assert response.status_code == 500
    assert "HX-Error-Page" not in response.headers


def test_the_fallback_page_carries_both_tracebacks():
    """When the error template itself cannot render, the fallback still has to
    say what happened - twice: the original failure and the one that hid it."""
    from episteme.web import errors

    original = ValueError("the original failure")
    rendering = RuntimeError("the template blew up")
    page = errors._fallback(500, rendering, original)
    assert "the original failure" in page
    assert "the template blew up" in page
    assert page.startswith("<!DOCTYPE html>")
