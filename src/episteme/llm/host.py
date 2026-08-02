"""Client for the host-side control agent (`hostagent/llama_agent.py`).

Deliberately a separate module from `gateway.py`. The gateway is the inference
choke point; process lifecycle is a different concern, and folding it in would
give a completion call the latent ability to spawn a GPU process on the host.
Nothing in the gateway imports this.

Two failure conventions, split by what the caller can do about it:

* **Reads** (`status`, `resources`, `logs`) return `None` when the feature is off
  or the agent is unreachable. A status panel must render a missing agent, not
  raise — and "the agent is down" is a normal state, since it is optional
  infrastructure that Episteme is designed to run without.
* **Actions** (`start`, `stop`, `restart`) raise `HostAgentError`. A start button
  that silently does nothing is worse than one that says why it failed.

The same split governs the timeouts, which is why there are three rather than
one. A read is a measurement someone is waiting on — the dashboard renders
`/status` on page load and polls `/logs` every 3s — so it must give up in
seconds; a hung agent that blocked those for the length of a model load would
take the admin page down with it. An action legitimately takes minutes: the
agent holds `/start` until both ports answer, and `/restart` pays for a stop
wait, a launcher run and a start wait in sequence, so it gets a ceiling of its
own rather than inheriting one sized for a single leg. A client timeout shorter
than the work it is waiting on renders a *success* as a failure — with the
processes running.

`llm_host_agent_url` empty is the off switch for all of it.
"""

import logging

import httpx

from ..config import settings

log = logging.getLogger("episteme.llm.host")


class HostAgentError(Exception):
    """The agent is off, unreachable, or refused the action."""


class HostAgent:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_url: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.llm_host_agent_url)

    def _http(self) -> httpx.AsyncClient:
        """One cached client, rebuilt if the configured URL changes (which it
        does between tests, and would on a config reload). Every call passes its
        own timeout, so the client-level one is only a backstop."""
        url = settings.llm_host_agent_url.rstrip("/")
        if self._client is None or self._client_url != url:
            self._client = httpx.AsyncClient(
                base_url=url, timeout=settings.llm_host_agent_read_timeout_seconds
            )
            self._client_url = url
        return self._client

    async def _request(self, method: str, path: str, *, timeout: float, **kwargs) -> dict:
        if not self.enabled:
            raise HostAgentError("No host agent configured (set LLM_HOST_AGENT_URL)")
        try:
            response = await self._http().request(method, path, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # transport, status, or an unparseable body
            raise HostAgentError(f"{type(exc).__name__}: {exc}") from exc

    async def _read(self, path: str, **kwargs) -> dict | None:
        try:
            return await self._request(
                "GET", path, timeout=settings.llm_host_agent_read_timeout_seconds, **kwargs
            )
        except HostAgentError as exc:
            log.debug("Host agent read %s failed: %s", path, exc)
            return None

    async def _action(self, path: str, *, timeout: float | None = None) -> dict:
        return await self._request(
            "POST", path, timeout=timeout or settings.llm_host_agent_timeout_seconds
        )

    # --- reads (degrade to None) --------------------------------------------

    async def status(self) -> dict | None:
        """Per-server listening/PID/uptime, or None if we can't ask."""
        return await self._read("/status")

    async def resources(self) -> dict | None:
        """GPU contention measurements. ~3.5s on the target box (the per-process
        GPU counter has an irreducible PDH sampling floor), so callers must not
        put this on a synchronous page load — the admin panel fetches it as its
        own htmx fragment and the governor polls it on a cron."""
        return await self._read("/resources")

    async def logs(self, which: str = "router", tail: int | None = None) -> dict | None:
        return await self._read(
            "/logs",
            params={"which": which, "tail": tail or settings.llm_log_tail_lines},
        )

    # --- actions (raise) -----------------------------------------------------

    async def start(self) -> dict:
        """Idempotent — the agent no-ops when both ports already answer, so two
        rapid clicks cannot produce two routers."""
        return await self._action("/start")

    async def stop(self) -> dict:
        """Kills the servers immediately. Callers wanting to not lose work should
        go through `web.api`'s graceful stop, which pauses the pipeline and waits
        for a unit boundary first."""
        return await self._action("/stop")

    async def restart(self) -> dict:
        """Its own timeout: the agent runs a stop (kill + port wait) and a start
        (launcher + port wait) inside one request, so the worst case is roughly
        the sum of the other two and comfortably exceeds the action ceiling. A
        restart that succeeded on the host but timed out here would be reported
        to the operator as a failure."""
        return await self._action(
            "/restart", timeout=settings.llm_host_agent_restart_timeout_seconds
        )


host_agent = HostAgent()
