"""Outbound notifications, over ntfy (0056).

One function, `publish`, and one rule: **a notification never fails the work it
reports on**. Every error is caught and logged, and the return value says whether
the push landed. A digest that cannot be delivered must not turn a good pipeline
run into a failed job, and a lunch menu that is already filed is still filed when
the phone is unreachable.

The **JSON publishing format**, not the header format, because `Title:` and
`Tags:` are HTTP headers and HTTP headers are ASCII. Today's menu says `Pannbiff
med pepparsas` on a good day and `Pannbiff med pepparsås` on a real one, and the
header form of that is either mojibake or a 400 from the server. The JSON body is
UTF-8 by definition, so the whole payload goes in one object posted to the base
URL, with the topic as a field rather than a path.

`ntfy_base_url` empty is the off switch, the same way `llm_host_agent_url` is for
the host agent: nothing is sent, nothing raises, and `enabled` is what callers
check when they want to skip building a message at all.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx

from .config import settings

log = logging.getLogger("episteme.notify")

# ntfy's scale: 1 min, 3 default, 5 max. Named because a call site saying
# `priority=2` is a number nobody can check, and these are the only three used.
LOW = 2  # a heartbeat nobody needs to act on
DEFAULT = 3
HIGH = 4  # something the reader would want to know before opening the app


def enabled() -> bool:
    return bool(settings.ntfy_base_url)


def link(path: str) -> str | None:
    """An absolute URL for `path` as the phone reaches this app, or None.

    `public_base_url` is the tailnet address, deliberately separate from
    `web_internal_url` (`http://web:8200`), which resolves only inside the docker
    network and would send every notification's tap to a dead host.
    """
    base = settings.public_base_url.strip()
    if not base:
        return None
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


async def publish(
    topic: str,
    title: str,
    message: str,
    *,
    tags: tuple[str, ...] = (),
    priority: int = DEFAULT,
    click: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Push one notification. Returns whether it was accepted.

    `tags` are ntfy's emoji shortcodes (`calendar`, `newspaper`), which is how one
    topic can carry two kinds of message and still be readable in the app's list.

    `transport` is the injection seam a test uses, the same one `LLMGateway` takes:
    what has to be checked here is the bytes on the wire, and a mock at the client
    level would check the arguments instead.
    """
    if not enabled():
        log.debug("No ntfy configured; dropping notification %r", title)
        return False
    payload: dict[str, object] = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
    }
    if tags:
        payload["tags"] = list(tags)
    if click:
        payload["click"] = click
    headers = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        async with httpx.AsyncClient(
            timeout=settings.ntfy_timeout_seconds, transport=transport
        ) as client:
            response = await client.post(
                settings.ntfy_base_url.rstrip("/") + "/", json=payload, headers=headers
            )
            response.raise_for_status()
    except Exception as exc:  # transport, auth, or a server that refused the topic
        # Warning, not an exception: see the module docstring. The job that called
        # this did its work, and this line is the only record that the reader was
        # not told about it.
        log.warning("ntfy publish failed (%s): %s", title, f"{type(exc).__name__}: {exc}")
        return False
    log.info("Notified %s: %s", topic, title)
    return True
