"""Client for InferMux's warden, the part of InferMux that watches the GPU (0060).

Deliberately a separate module from `gateway.py`. The gateway is the inference
choke point; reading the host's GPU and unloading its models is a different
concern, and nothing in the gateway imports this.

InferMux owns llama.cpp's lifecycle, logs and model configuration, and serves
them in its own UI. What Episteme asks it is narrow: what it decided and why
(`/warden/verdict`), a fresh GPU measurement (`/warden/resources`), which models
are loaded (`/running`), and one action, unload everything now.

Two failure conventions, split by what the caller can do about it:

* **Reads** return `None` when the feature is off or InferMux is unreachable. A
  status panel must render a missing warden, not raise, and "the warden is
  down" is a normal state, since Episteme runs against any OpenAI-compatible
  endpoint.
* **The action** raises `WardenError`. An unload button that silently does
  nothing is worse than one that says why it failed.

Every request carries this process's InferMux key (0058), the same one the
gateway sends. `llm_warden_url` empty is the off switch for all of it.
"""

import logging

import httpx

from ..config import settings

log = logging.getLogger("episteme.llm.warden")


class WardenError(Exception):
    """The warden is off, unreachable, or refused the action."""


class Warden:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_url: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.llm_warden_url)

    def _http(self) -> httpx.AsyncClient:
        """One cached client, rebuilt if the configured URL changes (which it
        does between tests). The key is read when the client is built, as the
        gateway does: a configured key file that is missing fails here, loudly."""
        url = settings.llm_warden_url.rstrip("/")
        if self._client is None or self._client_url != url:
            self._client = httpx.AsyncClient(
                base_url=url,
                headers=settings.llm_auth_headers(),
                timeout=settings.llm_warden_timeout_seconds,
            )
            self._client_url = url
        return self._client

    async def _request(self, method: str, path: str, *, timeout: float, **kwargs) -> dict:
        if not self.enabled:
            raise WardenError("No warden configured (set LLM_WARDEN_URL)")
        try:
            response = await self._http().request(method, path, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            # InferMux explains a refusal in `detail`, which says more than the
            # status line ("1 interactive request(s) in flight").
            try:
                detail = exc.response.json().get("detail")
            except ValueError:
                detail = None
            raise WardenError(detail or str(exc)) from exc
        except Exception as exc:  # transport, or an unparseable body
            raise WardenError(f"{type(exc).__name__}: {exc}") from exc

    async def _read(self, path: str) -> dict | None:
        try:
            return await self._request(
                "GET", path, timeout=settings.llm_warden_read_timeout_seconds
            )
        except WardenError as exc:
            log.debug("Warden read %s failed: %s", path, exc)
            return None

    # --- reads (degrade to None) --------------------------------------------

    async def verdict(self) -> dict | None:
        """What the warden last decided, and the policy it decided with.

        Cheap: the warden measures on its own clock and this is what it cached.
        `policy` is how the one "the GPU is busy" threshold reaches us without
        being copied into Episteme's settings (0057). Two numbers meaning the
        same thing would drift."""
        return await self._read("/warden/verdict")

    async def resources(self) -> dict | None:
        """A fresh measurement of the card and the processes on it. Fresh is the
        point, so callers keep it off synchronous page loads: the admin panel
        fetches it as its own htmx fragment, and a benchmark takes one at its run
        boundaries. `verdict()` carries the warden's last one for free."""
        return await self._read("/warden/resources")

    async def running(self) -> dict | None:
        """The models InferMux has loaded, from llama-swap's `/running`: one row
        per model with its `state` and `ttl`."""
        return await self._read("/running")

    # --- action (raises) -----------------------------------------------------

    async def unload(self) -> dict:
        """Unload every model now. InferMux refuses while an interactive request
        is in flight, and closes any batch session in flight, so callers that
        must not lose work go through `web.api`'s graceful unload, which pauses
        the pipeline and waits for a unit boundary first.

        Writes under /warden/ need the X-InferMux header, InferMux's guard
        against a browser being steered into one; any value passes."""
        return await self._request(
            "POST",
            "/warden/unload",
            timeout=settings.llm_warden_timeout_seconds,
            headers={"X-InferMux": "episteme"},
        )


warden = Warden()
