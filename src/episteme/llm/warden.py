"""Client for llama-warden, the process that owns llama.cpp on the host.

Deliberately a separate module from `gateway.py`. The gateway is the inference
choke point; process lifecycle is a different concern, and folding it in would
give a completion call the latent ability to spawn a GPU process on the host.
Nothing in the gateway imports this.

Two failure conventions, split by what the caller can do about it:

* **Reads** (`status`, `resources`, `logs`) return `None` when the feature is off
  or the agent is unreachable. A status panel must render a missing agent, not
  raise — and "the agent is down" is a normal state, since it is optional
  infrastructure that Episteme is designed to run without.
* **Actions** (`start`, `stop`, `restart`) raise `WardenError`. A start button
  that silently does nothing is worse than one that says why it failed.

The same split governs the timeouts, which is why there are three rather than
one. A read is a measurement someone is waiting on — the dashboard renders
`/status` on page load, and `/logs` is polled once a second for as long as
somebody has the log pane open — so it must give up in
seconds; a hung agent that blocked those for the length of a model load would
take the admin page down with it. An action legitimately takes minutes: the
agent holds `/start` until both ports answer, and `/restart` pays for a stop
wait, a launcher run and a start wait in sequence, so it gets a ceiling of its
own rather than inheriting one sized for a single leg. A client timeout shorter
than the work it is waiting on renders a *success* as a failure — with the
processes running.

`llm_warden_url` empty is the off switch for all of it.
"""

import logging

import httpx

from ..config import settings

log = logging.getLogger("episteme.llm.warden")


class WardenError(Exception):
    """The agent is off, unreachable, or refused the action."""


class Warden:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_url: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.llm_warden_url)

    def _http(self) -> httpx.AsyncClient:
        """One cached client, rebuilt if the configured URL changes (which it
        does between tests, and would on a config reload). Every call passes its
        own timeout, so the client-level one is only a backstop."""
        url = settings.llm_warden_url.rstrip("/")
        if self._client is None or self._client_url != url:
            self._client = httpx.AsyncClient(
                base_url=url, timeout=settings.llm_warden_read_timeout_seconds
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
        except Exception as exc:  # transport, status, or an unparseable body
            raise WardenError(f"{type(exc).__name__}: {exc}") from exc

    async def _read(self, path: str, **kwargs) -> dict | None:
        try:
            return await self._request(
                "GET", path, timeout=settings.llm_warden_read_timeout_seconds, **kwargs
            )
        except WardenError as exc:
            log.debug("Warden read %s failed: %s", path, exc)
            return None

    async def _action(
        self, path: str, *, timeout: float | None = None, json: dict | None = None
    ) -> dict:
        return await self._request(
            "POST",
            path,
            timeout=timeout or settings.llm_warden_timeout_seconds,
            **({"json": json} if json is not None else {}),
        )

    # --- reads (degrade to None) --------------------------------------------

    async def status(self) -> dict | None:
        """Per-server listening/PID/uptime, or None if we can't ask."""
        return await self._read("/status")

    async def resources(self) -> dict | None:
        """A FRESH measurement. ~3.5s on the target box (the per-process GPU
        counter has an irreducible PDH sampling floor), so callers must not put
        this on a synchronous page load — the admin panel fetches it as its own
        htmx fragment, and a benchmark takes one at its run boundaries.

        `verdict()` is the cheap read: it returns the warden's own last sweep
        with no new one taken. Ask for this only when a fresh number is the
        point."""
        return await self._read("/resources")

    async def verdict(self) -> dict | None:
        """What the warden last decided, and the policy it decided with.

        Cheap: the warden measures on its own 30s thread and this is what that
        thread cached, so nothing here pays the 3.5s probe. `policy` is how the
        one "the GPU is busy" threshold reaches us without being copied into
        Episteme's settings (0057) — two numbers meaning the same thing would
        drift, which is the argument 0024 made and the split did not change."""
        return await self._read("/verdict")

    async def logs(
        self, which: str = "router", tail: int | None = None, since: int | None = None
    ) -> dict | None:
        """`since` is a byte offset (the previous call's `next_offset`): the agent
        then returns only what was written after it, which is what lets the log
        pane append rather than re-render a whole tail. Omitted, it is a plain
        tail read. Sent only when set, so a tail read's request is unchanged."""
        params: dict[str, int | str] = {"which": which, "tail": tail or settings.llm_log_tail_lines}
        if since is not None:
            params["since"] = since
        return await self._read("/logs", params=params)

    # --- actions (raise) -----------------------------------------------------

    async def start(self, *, extra_args: list[str] | None = None) -> dict:
        """Idempotent — the agent no-ops when both ports already answer, so two
        rapid clicks cannot produce two routers.

        `extra_args` reaches llama-server through the launcher's passthrough. It
        exists for benchmark sweeps (0041) and is the *caller's* decision: the
        agent validates the characters and applies the list, it does not judge
        the configuration."""
        return await self._action("/start", json={"extra_args": extra_args or []})

    async def stop(self) -> dict:
        """Kills the servers immediately. Callers wanting to not lose work should
        go through `web.api`'s graceful stop, which pauses the pipeline and waits
        for a unit boundary first."""
        return await self._action("/stop")

    async def restart(self, *, extra_args: list[str] | None = None) -> dict:
        """Its own timeout: the agent runs a stop (kill + port wait) and a start
        (launcher + port wait) inside one request, so the worst case is roughly
        the sum of the other two and comfortably exceeds the action ceiling. A
        restart that succeeded on the host but timed out here would be reported
        to the operator as a failure."""
        return await self._action(
            "/restart",
            timeout=settings.llm_warden_restart_timeout_seconds,
            json={"extra_args": extra_args or []},
        )

    # --- model configuration (0041) ------------------------------------------
    #
    # A read that degrades to None and two actions that raise, same split as
    # above. `preset` is a read because a page showing the current configuration
    # must render without the agent; applying one is an action because a sweep
    # that silently failed to change anything would produce four identical
    # variants and a conclusion drawn from them.

    async def preset(self) -> dict | None:
        """The raw `models-preset.ini` text plus a parsed view of it."""
        return await self._read("/preset")

    async def apply_preset(self, sections: dict[str, dict]) -> dict:
        """`{section: {key: value_or_None}}`, None deleting a key. Returns the
        name of the backup taken first, which is the caller's obligation to hand
        back to `restore_preset` when the experiment ends. Deliberately not
        remembered by the agent: it is stateless (0023), and only the caller knows
        where a multi-variant sweep began."""
        return await self._action("/preset", json={"sections": sections})

    async def restore_preset(self, backup: str) -> dict:
        return await self._action("/preset/restore", json={"backup": backup})


warden = Warden()
