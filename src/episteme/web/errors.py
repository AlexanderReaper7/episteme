"""What a failing request shows instead of a bare `Internal Server Error`.

Three handlers, one page (`templates/error.html`). They are installed by
`install_error_handlers(app)` in `web.app`, and the split is by *audience*, not
by status code:

- an **HTML** request gets the page: status, the exception line, the innermost
  frame that is ours, the request that produced it, every frame with its source
  line, and the raw traceback text to paste somewhere;
- an **API** request keeps exactly the JSON body FastAPI already sent, because
  `detail` is a contract other code reads. Detail is *added* alongside it,
  never substituted for it.

Full tracebacks go to the browser deliberately. Episteme is single-user with no
auth and is reached over a tailnet, so there is no audience to withhold them
from, and the alternative — an opaque 500 plus a `docker compose logs` round
trip — is the thing being removed (0043).

The handlers must not themselves raise: an error page that 500s leaves nothing
at all on screen. `_render` therefore falls back to a self-contained document
built with no template, no CSS and no context.
"""

from __future__ import annotations

import html
import traceback
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from jinja2_fragments import render_block

from .templating import templates

# Everything under here is ours; anything else is a dependency frame. Templates
# live inside the package, so a Jinja frame classifies as app code for free.
_SRC_ROOT = Path(__file__).resolve().parents[2]

# htmx targets that mean "this request is a navigation" — the only case where an
# error page can be swapped in without destroying the page the reader is on. A
# polled fragment (the job queue, the log pane) failing must leave the document
# alone, so it gets the page as a plain unswappable response and htmx's default
# error handling.
_NAV_TARGETS = frozenset({"main-content", "admin-main"})

# The response header app.js keys on to let htmx swap a non-2xx body. Servers
# opting in per-response, rather than a global `htmx.config.responseHandling`
# rule, is what keeps a failing background poll from blanking the page.
_ERROR_PAGE_HEADER = "HX-Error-Page"


@dataclass(frozen=True)
class Frame:
    """One traceback frame, already shaped for display."""

    file: str
    line: int
    name: str
    source: str
    app: bool


def _display_path(filename: str) -> tuple[str, bool]:
    """A short path for one frame, and whether the frame is Episteme's own.

    Ours are shown relative to `src/`; a dependency keeps only the part after
    `site-packages/`, which is the part that identifies the library.
    """
    try:
        resolved = Path(filename).resolve()
    except (OSError, ValueError):
        return filename, False
    try:
        return resolved.relative_to(_SRC_ROOT).as_posix(), True
    except ValueError:
        pass
    parts = resolved.as_posix().split("/site-packages/")
    return (parts[-1], False) if len(parts) > 1 else (resolved.as_posix(), False)


def _frames(exc: BaseException) -> list[Frame]:
    """The frames of `exc` itself, innermost last, as the page renders them.

    Only this exception's own stack, not the chain: a `raise ... from exc`
    re-raise would otherwise bury the frames that matter under the ones that
    re-raised them. The full chain is still in the raw traceback below the
    table, which is where a cause belongs.
    """
    summary = traceback.TracebackException(
        type(exc), exc, exc.__traceback__, capture_locals=False
    ).stack
    frames = []
    for entry in summary:
        file, app = _display_path(entry.filename)
        frames.append(
            Frame(
                file=file,
                line=entry.lineno or 0,
                name=entry.name,
                source=(entry.line or "").strip(),
                app=app,
            )
        )
    return frames


def _origin(frames: list[Frame]) -> Frame | None:
    """The innermost frame that is ours — where to look first.

    A Jinja `UndefinedError` bottoms out in `jinja2/environment.py`, and a
    SQLAlchemy error in the driver; neither is where the mistake is. The last
    app frame is, and for a template error that frame is the template line.
    """
    return next((frame for frame in reversed(frames) if frame.app), None)


def _request_facts(request: Request) -> list[tuple[str, str]]:
    """The request, as label/value rows. Only what is present is listed."""
    rows = [("Method", request.method), ("Path", request.url.path)]
    if request.url.query:
        rows.append(("Query", request.url.query))
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None) != request.url.path:
        rows.append(("Route", getattr(route, "path", "")))
    if request.path_params:
        rows.append(("Path params", repr(request.path_params)))
    for header in ("hx-request", "hx-boosted", "hx-target", "hx-trigger"):
        value = request.headers.get(header)
        if value:
            rows.append((header.upper(), value))
    if request.client:
        rows.append(("Client", request.client.host))
    return rows


def _fragment(request: Request) -> bool:
    """Whether this failure should come back as a swappable fragment.

    True only for a boosted navigation, which is how nearly every page in this
    app is actually reached — an unswapped error there is a click that visibly
    does nothing at all.
    """
    if request.headers.get("HX-Request") != "true":
        return False
    return request.headers.get("HX-Target", "") in _NAV_TARGETS


def _wants_json(request: Request) -> bool:
    """`/api` is JSON; everything else is a person looking at a browser.

    Keyed off the path rather than `Accept`, because the browser sends
    `Accept: */*` for a `fetch` and htmx sends it for every swap — a header that
    cannot separate the two cannot decide this.
    """
    path = request.url.path
    return path == "/api" or path.startswith("/api/")


def _summary(exc: BaseException | None) -> str:
    """The one line under the status: what went wrong, in the words of whatever
    raised it.

    An `HTTPException` says it in `detail`; a validation failure says it in a
    list of per-field dicts, which is unreadable as a repr and is flattened to
    `body.limit: value is not a valid integer`; anything else says it in `str`.
    """
    if exc is None:
        return ""
    if isinstance(exc, RequestValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
            for error in exc.errors()
        )
    detail = getattr(exc, "detail", None)
    return str(detail) if detail is not None else str(exc)


def _context(request: Request, status: int, exc: BaseException | None) -> dict:
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Error"
    frames = _frames(exc) if exc is not None else []
    # A bare `HTTPException(404)` has FastAPI's own status phrase as its detail,
    # so the summary line would repeat the heading verbatim. Drop it rather than
    # print "Not Found / Not Found" — the line means "and here is what it says",
    # and it should only appear when something was actually said.
    summary = _summary(exc)
    return {
        "status": status,
        "reason": reason,
        "summary": "" if summary == reason else summary,
        "exc_type": (f"{type(exc).__module__}.{type(exc).__qualname__}" if exc else ""),
        "origin": _origin(frames),
        "frames": frames,
        "facts": _request_facts(request),
        "raw": ("".join(traceback.format_exception(exc)).rstrip() if exc is not None else ""),
    }


def _fallback(status: int, context_error: BaseException, exc: BaseException | None) -> str:
    """The page when the page itself failed to render. No template, no CSS, no
    context — the two tracebacks and nothing that can fail."""
    original = "".join(traceback.format_exception(exc)) if exc is not None else ""
    rendering = "".join(traceback.format_exception(context_error))
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="color-scheme" content="dark">'
        f"<title>Episteme {status}</title></head>"
        '<body style="background:#000;color:#e8e6e1;font-family:ui-monospace,monospace;padding:2rem">'
        f"<h1>{status}</h1>"
        "<p>The error page itself failed to render. Both tracebacks:</p>"
        f"<h2>Original</h2><pre>{html.escape(original)}</pre>"
        f"<h2>While rendering the error page</h2><pre>{html.escape(rendering)}</pre>"
        "</body></html>"
    )


def _render(request: Request, status: int, exc: BaseException | None) -> HTMLResponse:
    """The error page, as a document or as a swappable fragment."""
    fragment = _fragment(request)
    try:
        context = {"request": request, **_context(request, status, exc)}
        if not fragment:
            return templates.TemplateResponse(request, "error.html", context, status_code=status)
        title = render_block(templates.env, "error.html", "title", **context)
        body = render_block(templates.env, "error.html", "content", **context)
        return HTMLResponse(
            f"<title>Episteme{title}</title>\n{body}",
            status_code=status,
            headers={
                # Every error lands in the top-level outlet, whichever outlet
                # asked: `error.html` has a `content` block and no
                # `admin_content` one, so an intra-admin navigation that fails
                # replaces the admin shell rather than half-filling it.
                "HX-Retarget": "#main-content",
                "HX-Reswap": "innerHTML",
                _ERROR_PAGE_HEADER: "true",
            },
        )
    except Exception as render_error:  # noqa: BLE001 - the whole point of a fallback
        return HTMLResponse(_fallback(status, render_error, exc), status_code=status)


async def _unhandled(request: Request, exc: Exception):
    """Anything a route let escape. Starlette re-raises after this returns, so
    uvicorn still logs the traceback — the page is in addition to the log, not
    instead of it."""
    if _wants_json(request):
        return await http_exception_handler(request, HTTPException(500, "Internal Server Error"))
    return _render(request, 500, exc)


async def _http_error(request: Request, exc: HTTPException):
    if _wants_json(request):
        return await http_exception_handler(request, exc)
    return _render(request, exc.status_code, exc)


async def _validation_error(request: Request, exc: RequestValidationError):
    """A bad query string or form field. FastAPI's own 422 body is a list of
    per-field errors, which is worth keeping verbatim for `/api`; an HTML route
    gets the same list rendered as the page's summary."""
    if _wants_json(request):
        return await request_validation_exception_handler(request, exc)
    return _render(request, 422, exc)


def install_error_handlers(app) -> None:
    """Wire the three handlers onto the app. Called once, from `web.app`."""
    app.add_exception_handler(Exception, _unhandled)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
