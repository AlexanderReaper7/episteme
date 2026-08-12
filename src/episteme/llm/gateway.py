"""LLM gateway — the single module through which all model access flows.

Code asks for a *role* (`main`, `fast`, `embed`, `chat`); config maps each role
to a model name **and a base URL** on an OpenAI-compatible endpoint (spec §7).
Structured output is enforced twice: the JSON schema is sent as a
`response_format` so llama.cpp constrains generation grammatically, and the
response is validated with the same pydantic model client-side, with
repair-prompt retries.

Endpoint topology is config, not code. Every role resolves through
`endpoint_for()` to a URL (`llm_<role>_base_url`, falling back to
`llm_base_url`), and one client is cached per distinct URL — so roles sharing a
server share a connection pool, and moving one role to its own llama-server is
an env change. Nothing here knows that `embed` is the role that happens to sit
on its own port today; health, the admin view and unload all derive the split
from the resolved URLs.
"""

import json
import logging
import math
import time
from collections.abc import AsyncIterator
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import settings
from ..models import EMBEDDING_DIM
from .observe import record_llm_call

log = logging.getLogger("episteme.llm")

Role = Literal["main", "fast", "embed", "chat"]
ROLES: tuple[Role, ...] = ("main", "fast", "embed", "chat")

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


def _as_llm_error(exc: Exception) -> Exception:
    """Transport failures reach callers as `LLMError`, like every other way an LLM
    call can fail.

    The gateway is the single choke point for model access, so `LLMError` is the
    contract callers write their fallbacks against — `_name_clusters` degrades to
    member names, triage skips the story, the nightly stage catches up. A raw
    `httpx.ReadTimeout` slipping past that contract is not a different kind of
    problem, only a differently-typed one, and it takes the fallback with it.

    Found live (2026-07-29): the local router had to swap the fast model in, the
    request sat the full 600s timeout without returning a byte, and the
    `httpx.ReadTimeout` blew through `_name_clusters`' handler and failed the whole
    `propose_topics` job — where the handler existed precisely so a naming failure
    would degrade to member names instead. Timeouts against a single-GPU box that
    loads models on demand are ordinary, not exceptional.

    A malformed 200 is the same class of problem and is covered too: llama-server
    can answer with a body that is not JSON, or JSON without `choices` (an error
    object, a truncated stream, a proxy's own page). Leaving the parse outside the
    guarded region would have left exactly one route — the response arriving but
    being unusable — that still bypasses every `except LLMError` in the codebase.
    """
    if isinstance(exc, httpx.HTTPError | ValueError | KeyError | IndexError | TypeError):
        return LLMError(f"{type(exc).__name__}: {exc}")
    return exc


# llama.cpp's schema→grammar converter emits bounded repetitions its own GBNF
# parser rejects once maxLength reaches 2000 ("failed to parse grammar", HTTP 400
# — measured: 1999 compiles, 2000 doesn't). Constraints the grammar layer cannot
# express are dropped from the server-sent schema only; pydantic still enforces
# the real limit client-side, backed by the repair-prompt retries.
GRAMMAR_MAX_STRING_LENGTH = 1000


def _normalize(url: str) -> str:
    """Endpoint identity is the URL, so trailing-slash variants of one server must
    not read as two endpoints (double probes, two connection pools, a duplicate
    admin row)."""
    return url.rstrip("/")


def holds_vram(model: dict) -> bool:
    """Does this `/models` row currently occupy VRAM?

    The single definition, because two callers ask it for opposite purposes —
    `unload_models` ("is there anything to hand back?") and `models_loaded`
    ("may we read free VRAM as someone else's?") — and a disagreement between
    them is a bug in whichever one is more optimistic.

    Only llama-server in *router* mode reports a per-model `status.value`; a
    plain single-model server reports nothing, which counts as holding nothing.
    Everything else counts as loaded, and that deliberately includes the
    transient `loading`: a model halfway into VRAM occupies it just as much as a
    resident one, and reading it as free is what let the governor pause and
    unload the model it was in the middle of loading."""
    return ((model.get("status") or {}).get("value")) not in (None, "unloaded")


class StreamAccumulator:
    """Rebuilds one complete assistant message from `chat/completions` deltas.

    A streamed response is the same message as a non-streamed one, taken apart.
    This puts it back together so that `chat_stream`'s final product is
    shape-identical to what `chat_messages` returns — which is the property that
    lets `run_tool_loop`, `observe`, and every caller stay ignorant of streaming.

    The part that is not obvious: **tool-call arguments arrive as string
    fragments across chunks**, keyed by `index`, and only the FIRST fragment
    carries `id` and `function.name`. Concatenating per index is the whole
    algorithm; appending per *arrival* instead would interleave two parallel
    tool calls into one unparseable JSON string.

    A separate class rather than a closure so it is unit-testable against
    recorded chunk sequences without an HTTP layer.
    """

    def __init__(self) -> None:
        self._content: list[str] = []
        self._tool_calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: dict = {}

    def feed(self, chunk: dict) -> str:
        """Absorb one parsed `data:` payload; return the text delta it carried
        (empty string when it carried none), so the caller can forward it."""
        self.usage = chunk.get("usage") or self.usage
        choices = chunk.get("choices") or []
        if not choices:
            # Usage-only final chunk (stream_options.include_usage).
            return ""
        choice = choices[0]
        self.finish_reason = choice.get("finish_reason") or self.finish_reason
        delta = choice.get("delta") or {}
        for call in delta.get("tool_calls") or []:
            slot = self._tool_calls.setdefault(
                call.get("index", 0),
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if call.get("id"):
                slot["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] = function["name"]
            # `or ""` and not `.get(..., "")`: a chunk may carry an explicit null.
            slot["function"]["arguments"] += function.get("arguments") or ""
        text = delta.get("content") or ""
        if text:
            self._content.append(text)
        return text

    def message(self) -> dict:
        """The assembled assistant message. `content` is None rather than "" when
        empty, matching what a non-streaming server sends alongside tool calls."""
        content = "".join(self._content)
        message: dict = {"role": "assistant", "content": content or None}
        if self._tool_calls:
            message["tool_calls"] = [self._tool_calls[i] for i in sorted(self._tool_calls)]
        return message


def grammar_safe(schema: object) -> object:
    """Deep-copy `schema` with grammar-incompilable constraints removed."""
    if isinstance(schema, dict):
        return {
            key: grammar_safe(value)
            for key, value in schema.items()
            if not (key == "maxLength" and isinstance(value, int) and value > GRAMMAR_MAX_STRING_LENGTH)
        }
    if isinstance(schema, list):
        return [grammar_safe(item) for item in schema]
    return schema


class LLMGateway:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        # One client per distinct URL, built on demand. Keyed by the resolved URL
        # rather than by role, so two roles on one server share a connection pool
        # and moving a role elsewhere costs nothing here.
        self._clients: dict[str, httpx.AsyncClient] = {}

    # --- endpoint resolution ------------------------------------------------

    def endpoint_for(self, role: Role) -> str:
        """The base URL serving `role`: its own override, else the default.

        `chat` falls back through `main` rather than straight to the default,
        because "the assistant runs wherever the writer runs" is the property that
        keeps an interactive turn from triggering a model swap. Moving main to its
        own server would otherwise silently leave chat behind on the shared one."""
        override = {
            "main": settings.llm_main_base_url,
            "fast": settings.llm_fast_base_url,
            "embed": settings.llm_embed_base_url,
            "chat": settings.llm_chat_base_url or settings.llm_main_base_url,
        }[role]
        return _normalize(override or settings.llm_base_url)

    def endpoints(self) -> dict[str, list[Role]]:
        """Distinct URL -> the roles it serves, in role order. Two roles collapse
        into one row exactly when they resolve to the same URL, so callers never
        double-probe a shared server."""
        grouped: dict[str, list[Role]] = {}
        for role in ROLES:
            grouped.setdefault(self.endpoint_for(role), []).append(role)
        return grouped

    def _client(self, url: str) -> httpx.AsyncClient:
        url = _normalize(url)
        client = self._clients.get(url)
        if client is None:
            client = httpx.AsyncClient(
                base_url=url, timeout=settings.llm_timeout_seconds, transport=self._transport
            )
            self._clients[url] = client
        return client

    def client_for(self, role: Role) -> httpx.AsyncClient:
        return self._client(self.endpoint_for(role))

    def _configured_model(self, role: Role) -> str:
        return {
            "main": settings.llm_model_main,
            "fast": settings.llm_model_fast,
            "embed": settings.llm_model_embed,
            "chat": settings.llm_model_chat or settings.llm_model_main,
        }[role]

    def model_for(self, role: Role) -> str:
        model = self._configured_model(role)
        if not model:
            raise LLMError(f"No model configured for role {role!r} (see LLM_MODEL_* env)")
        return model

    def role_config(self) -> dict[Role, dict[str, str]]:
        """The whole role table — model and endpoint per role — for the admin view.
        Reads the same settings `model_for`/`endpoint_for` do, but reports an
        unconfigured role as an empty string instead of raising: a status page must
        render a broken configuration, not fail on it."""
        return {
            role: {"model": self._configured_model(role), "url": self.endpoint_for(role)}
            for role in ROLES
        }

    # --- health / inventory -------------------------------------------------

    async def _probe(self, url: str) -> list[dict] | None:
        """One `/models` GET: the endpoint's health and its inventory are the same
        question, so they are the same request. `None` = the endpoint is down."""
        try:
            response = await self._client(url).get("/models", timeout=5.0)
            if response.status_code != 200:
                return None
            return response.json().get("data", [])
        except Exception:
            return None

    async def unavailable_endpoints(self) -> list[str]:
        """Which configured endpoints are not answering; empty = everything is up.

        The availability gate wants a bool and the log line wants a name, so this
        returns the names and lets truthiness answer the bool — one probe, one
        definition of "up". Every distinct endpoint counts: a run that can't embed
        is doomed anyway, and which server that is depends on config."""
        return [url for url in self.endpoints() if await self._probe(url) is None]

    async def list_models(self, url: str | None = None) -> list[dict]:
        """Raw /models rows (llama-server router mode includes load state) from one
        endpoint, defaulting to the shared base URL."""
        try:
            response = await self._client(url or settings.llm_base_url).get("/models", timeout=5.0)
            response.raise_for_status()
            return response.json().get("data", [])
        except Exception as exc:
            raise _as_llm_error(exc) from exc

    async def endpoint_status(self) -> list[dict]:
        """Per-distinct-endpoint health + inventory for the admin dashboard: one
        row per URL we talk to, each independently probed, tagged with the roles it
        serves. No endpoint is privileged — a single-server config yields one row
        carrying all three roles."""
        rows = []
        for url, roles in self.endpoints().items():
            models = await self._probe(url)
            rows.append(
                {
                    "url": url,
                    "roles": roles,
                    "available": models is not None,
                    "models": [
                        {"id": m.get("id"), "status": (m.get("status") or {}).get("value")}
                        for m in (models or [])
                    ],
                }
            )
        return rows

    async def models_loaded(self) -> bool:
        """Is any endpoint holding a model in VRAM right now?

        Shares `holds_vram` with `unload_models`, deliberately: the two are the
        same question asked by different callers, and when they disagreed the
        governor read a model *loading* as "we hold nothing", attributed our own
        fresh allocation to someone else, and paused + unloaded the model it was
        in the middle of loading.

        A down endpoint holds nothing (`_probe` returns None), which is the same
        no-opinion direction the rest of this feature takes."""
        for url in self.endpoints():
            if any(holds_vram(row) for row in (await self._probe(url)) or []):
                return True
        return False

    async def unload_models(self) -> list[str]:
        """Ask every endpoint to unload the models it currently has loaded, freeing
        VRAM for other applications (pause support). Servers load lazily, so no
        matching "load" is needed — the next request reloads.

        No endpoint is skipped by role. Only llama-server in *router* mode reports
        a `status.value` per model, so a plain single-model server (the embed one
        today) reports none loaded and is never sent an unload — the server's own
        answer decides, not our idea of which port holds VRAM. Best-effort
        throughout: failures are logged, not raised.

        The unload route lives at the server root, not under /v1."""
        unloaded: list[str] = []
        for url in self.endpoints():
            client = self._client(url)
            root = url.removesuffix("/v1")
            try:
                for row in await self.list_models(url):
                    if not holds_vram(row):
                        continue
                    response = await client.post(
                        f"{root}/models/unload", json={"model": row["id"]}, timeout=30.0
                    )
                    if response.status_code == 200:
                        unloaded.append(row["id"])
                    else:
                        log.warning("Unload of %s failed: %s", row["id"], response.text[:200])
            except (httpx.HTTPError, LLMError) as exc:
                log.warning("Model unload on %s failed: %s", url, exc)
        return unloaded

    async def chat(
        self,
        role: Role,
        system: str,
        user: str,
        response_schema: dict | None = None,
        temperature: float = 0.3,
    ) -> str:
        if response_schema is not None:
            response_schema = grammar_safe(response_schema)
        payload: dict = {
            "model": self.model_for(role),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if settings.llm_disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": response_schema},
            }
        client = self.client_for(role)
        start = time.monotonic()
        try:
            response = await client.post("/chat/completions", json=payload)
            if response.status_code == 400 and response_schema is not None:
                # Older llama.cpp builds use the pre-OpenAI "json_object" + schema form.
                payload["response_format"] = {"type": "json_object", "schema": response_schema}
                response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            # Parsing belongs INSIDE the guard: a 200 whose body is not the JSON
            # we expect is a failed call like any other, and must reach callers as
            # LLMError rather than as a JSONDecodeError nothing catches.
            data = response.json()
            message = data["choices"][0]["message"]
            content = message["content"]
        except Exception as exc:
            await record_llm_call(
                role=role,
                model=payload["model"],
                kind="chat",
                duration_ms=int((time.monotonic() - start) * 1000),
                request={"messages": payload["messages"], "constrained": response_schema is not None},
                error=str(exc),
            )
            raise _as_llm_error(exc) from exc
        usage = data.get("usage") or {}
        log.info(
            "%s completion: %s prompt + %s completion tokens",
            role,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        await record_llm_call(
            role=role,
            model=payload["model"],
            kind="chat",
            duration_ms=int((time.monotonic() - start) * 1000),
            request={"messages": payload["messages"], "constrained": response_schema is not None},
            response=message,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return content

    async def chat_messages(
        self,
        role: Role,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_schema: dict | None = None,
        temperature: float = 0.3,
    ) -> dict:
        """Lower-level chat over a full message list, optionally with tools and/or a
        grammar-constraining response schema. Returns the raw assistant message dict
        (may carry `tool_calls`). Used by the writer's agentic loop (the schema turn
        produces the final draft inside the same conversation); `chat`/`complete_json`
        remain the path for single-shot calls."""
        if response_schema is not None:
            response_schema = grammar_safe(response_schema)
        payload: dict = {
            "model": self.model_for(role),
            "messages": messages,
            "temperature": temperature,
        }
        if settings.llm_disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": response_schema},
            }
        client = self.client_for(role)
        start = time.monotonic()
        try:
            response = await client.post("/chat/completions", json=payload)
            if response.status_code == 400 and response_schema is not None:
                # Older llama.cpp builds use the pre-OpenAI "json_object" + schema form.
                payload["response_format"] = {"type": "json_object", "schema": response_schema}
                response = await client.post("/chat/completions", json=payload)
            response.raise_for_status()
            # Inside the guard for the same reason as `chat` above.
            data = response.json()
            message = data["choices"][0]["message"]
        except Exception as exc:
            await record_llm_call(
                role=role,
                model=payload["model"],
                kind="tool-chat",
                duration_ms=int((time.monotonic() - start) * 1000),
                request={"messages": messages, "tools": bool(tools)},
                error=str(exc),
            )
            raise _as_llm_error(exc) from exc
        usage = data.get("usage") or {}
        log.info(
            "%s tool-chat: %s prompt + %s completion tokens",
            role,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        await record_llm_call(
            role=role,
            model=payload["model"],
            kind="tool-chat",
            duration_ms=int((time.monotonic() - start) * 1000),
            request={"messages": messages, "tools": bool(tools)},
            response=message,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return message

    async def chat_stream(
        self,
        role: Role,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
    ) -> AsyncIterator[dict]:
        """`chat_messages`, streamed. Yields `{"type": "text", "delta": str}` as
        tokens arrive and finally exactly one `{"type": "message", "message": {...}}`
        carrying the assembled assistant message, identical in shape to what
        `chat_messages` returns.

        The terminal event is how a caller gets the result: a bare async generator
        has no return value an `async for` can see. Callers that only want the
        finished message should use `chat_messages`; this exists for the one caller
        with a human watching (`llm/chat.py`), because a tool loop on the main model
        behind a model swap is minutes of silence otherwise.

        Observability is unchanged: one `record_llm_call` at the end, from the
        assembled message, so a streamed turn and a blocking one are the same row.
        `LLMError` remains the whole contract, including for a stream that dies
        halfway — the partial text already yielded is the caller's to discard.
        """
        payload: dict = {
            "model": self.model_for(role),
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            # Streamed responses carry no usage block by default; without this the
            # llm_calls row for every chat turn would have null token counts.
            "stream_options": {"include_usage": True},
        }
        if settings.llm_disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        accumulator = StreamAccumulator()
        start = time.monotonic()
        request = {"messages": messages, "tools": bool(tools), "streamed": True}
        try:
            async with self.client_for(role).stream(
                "POST", "/chat/completions", json=payload
            ) as response:
                if response.status_code != 200:
                    # The body has not been read yet on a streaming response, and
                    # raise_for_status would report the status with no detail.
                    await response.aread()
                    response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # blank separators and `:` keep-alive comments
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    delta = accumulator.feed(json.loads(data))
                    if delta:
                        yield {"type": "text", "delta": delta}
        except Exception as exc:
            await record_llm_call(
                role=role,
                model=payload["model"],
                kind="tool-chat",
                duration_ms=int((time.monotonic() - start) * 1000),
                request=request,
                error=str(exc),
            )
            raise _as_llm_error(exc) from exc

        message = accumulator.message()
        usage = accumulator.usage
        log.info(
            "%s stream: %s prompt + %s completion tokens",
            role,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        await record_llm_call(
            role=role,
            model=payload["model"],
            kind="tool-chat",
            duration_ms=int((time.monotonic() - start) * 1000),
            request=request,
            response=message,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        yield {"type": "message", "message": message}

    async def complete_json(
        self,
        role: Role,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.3,
    ) -> T:
        """Chat completion parsed and validated into `schema`, with repair retries."""
        json_schema = schema.model_json_schema()
        prompt = user
        last_error: Exception | None = None
        for attempt in range(1 + settings.llm_max_json_retries):
            content = await self.chat(
                role, system, prompt, response_schema=json_schema, temperature=temperature
            )
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
                log.warning(
                    "Invalid %s output (attempt %d): %s", schema.__name__, attempt + 1, exc
                )
                prompt = (
                    f"{user}\n\nYour previous response failed validation with:\n{exc}\n"
                    "Respond again with ONLY valid JSON matching the schema."
                )
        raise LLMError(f"{schema.__name__} failed validation after retries: {last_error}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeddings truncated to EMBEDDING_DIM and re-normalized (Matryoshka-style),
        so the DB column stays valid across embedding-model swaps."""
        if not texts:
            return []
        start = time.monotonic()
        try:
            response = await self.client_for("embed").post(
                "/embeddings", json={"model": self.model_for("embed"), "input": texts}
            )
            response.raise_for_status()
            # Inside the guard for the same reason as `chat` above — and so a
            # dimension mismatch is recorded as the failed call it is.
            rows = sorted(response.json()["data"], key=lambda r: r["index"])
            vectors = [_truncate_normalize(row["embedding"]) for row in rows]
        except Exception as exc:
            await record_llm_call(
                role="embed",
                model=self.model_for("embed"),
                kind="embed",
                duration_ms=int((time.monotonic() - start) * 1000),
                request={"batch_size": len(texts)},
                error=str(exc),
            )
            raise _as_llm_error(exc) from exc
        # Batch size only — 300+ full payloads a night would drown the log.
        await record_llm_call(
            role="embed",
            model=self.model_for("embed"),
            kind="embed",
            duration_ms=int((time.monotonic() - start) * 1000),
            request={"batch_size": len(texts)},
        )
        return vectors


def _truncate_normalize(vector: list[float]) -> list[float]:
    if len(vector) < EMBEDDING_DIM:
        raise LLMError(
            f"Embedding model returns {len(vector)} dims; need >= {EMBEDDING_DIM}. "
            "Configure a larger embedding model or lower EMBEDDING_DIM."
        )
    head = vector[:EMBEDDING_DIM]
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


gateway = LLMGateway()
