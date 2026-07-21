"""LLM gateway — the single module through which all model access flows.

Code asks for a *role* (`main`, `fast`, `embed`); config maps each role to a
model name on an OpenAI-compatible endpoint (spec §7). Structured output is
enforced twice: the JSON schema is sent as a `response_format` so llama.cpp
constrains generation grammatically, and the response is validated with the
same pydantic model client-side, with repair-prompt retries.
"""

import logging
import math
import time
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import settings
from ..models import EMBEDDING_DIM
from .observe import record_llm_call

log = logging.getLogger("episteme.llm")

Role = Literal["main", "fast", "embed"]

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


# llama.cpp's schema→grammar converter emits bounded repetitions its own GBNF
# parser rejects once maxLength reaches 2000 ("failed to parse grammar", HTTP 400
# — measured: 1999 compiles, 2000 doesn't). Constraints the grammar layer cannot
# express are dropped from the server-sent schema only; pydantic still enforces
# the real limit client-side, backed by the repair-prompt retries.
GRAMMAR_MAX_STRING_LENGTH = 1000


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
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            transport=transport,
        )
        # Embeds may live on a separate always-resident server (see config).
        self._embed_client = httpx.AsyncClient(
            base_url=settings.llm_embed_base_url,
            timeout=settings.llm_timeout_seconds,
            transport=transport,
        )

    def model_for(self, role: Role) -> str:
        model = {
            "main": settings.llm_model_main,
            "fast": settings.llm_model_fast,
            "embed": settings.llm_model_embed,
        }[role]
        if not model:
            raise LLMError(f"No model configured for role {role!r} (see LLM_MODEL_* env)")
        return model

    async def is_available(self) -> bool:
        """Both endpoints must answer — a run that can't embed is doomed anyway."""
        try:
            response = await self._client.get("/models", timeout=5.0)
            if response.status_code != 200:
                return False
            if settings.llm_embed_base_url != settings.llm_base_url:
                response = await self._embed_client.get("/models", timeout=5.0)
                return response.status_code == 200
            return True
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[dict]:
        """Raw /models rows (llama-server router mode includes load state)."""
        response = await self._client.get("/models", timeout=5.0)
        response.raise_for_status()
        return response.json().get("data", [])

    async def endpoint_status(self) -> list[dict]:
        """Per-distinct-endpoint health for the admin dashboard: one row per URL
        we talk to, each independently probed, tagged with the roles it serves.
        The embed server is a separate row only when it lives on a different URL."""

        async def _ok(client: httpx.AsyncClient) -> bool:
            try:
                response = await client.get("/models", timeout=5.0)
                return response.status_code == 200
            except httpx.HTTPError:
                return False

        endpoints = [
            {
                "url": settings.llm_base_url,
                "roles": ["main", "fast"],
                "available": await _ok(self._client),
            }
        ]
        if settings.llm_embed_base_url != settings.llm_base_url:
            endpoints.append(
                {
                    "url": settings.llm_embed_base_url,
                    "roles": ["embed"],
                    "available": await _ok(self._embed_client),
                }
            )
        else:
            endpoints[0]["roles"].append("embed")
        return endpoints

    async def unload_models(self) -> list[str]:
        """Ask the router to unload every loaded decode model, freeing VRAM for
        other applications (pause support). The router loads models lazily, so no
        matching "load" is needed — the next completion request reloads. The
        dedicated embed server is untouched: always-resident by design, and it
        holds no VRAM (--device none). Best-effort: failures are logged, not raised.

        Router-mode endpoint lives at the server root, not under /v1."""
        root = settings.llm_base_url.removesuffix("/v1")
        unloaded: list[str] = []
        try:
            for row in await self.list_models():
                status = row.get("status") or {}
                if status.get("value") not in (None, "unloaded"):
                    response = await self._client.post(
                        f"{root}/models/unload", json={"model": row["id"]}, timeout=30.0
                    )
                    if response.status_code == 200:
                        unloaded.append(row["id"])
                    else:
                        log.warning("Unload of %s failed: %s", row["id"], response.text[:200])
        except httpx.HTTPError as exc:
            log.warning("Model unload failed: %s", exc)
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
        start = time.monotonic()
        try:
            response = await self._client.post("/chat/completions", json=payload)
            if response.status_code == 400 and response_schema is not None:
                # Older llama.cpp builds use the pre-OpenAI "json_object" + schema form.
                payload["response_format"] = {"type": "json_object", "schema": response_schema}
                response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
        except Exception as exc:
            await record_llm_call(
                role=role,
                model=payload["model"],
                kind="chat",
                duration_ms=int((time.monotonic() - start) * 1000),
                request={"messages": payload["messages"], "constrained": response_schema is not None},
                error=str(exc),
            )
            raise
        data = response.json()
        usage = data.get("usage", {})
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
            response=data["choices"][0]["message"],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return data["choices"][0]["message"]["content"]

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
        start = time.monotonic()
        try:
            response = await self._client.post("/chat/completions", json=payload)
            if response.status_code == 400 and response_schema is not None:
                # Older llama.cpp builds use the pre-OpenAI "json_object" + schema form.
                payload["response_format"] = {"type": "json_object", "schema": response_schema}
                response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
        except Exception as exc:
            await record_llm_call(
                role=role,
                model=payload["model"],
                kind="tool-chat",
                duration_ms=int((time.monotonic() - start) * 1000),
                request={"messages": messages, "tools": bool(tools)},
                error=str(exc),
            )
            raise
        data = response.json()
        usage = data.get("usage", {})
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
            response=data["choices"][0]["message"],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return data["choices"][0]["message"]

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
            response = await self._embed_client.post(
                "/embeddings", json={"model": self.model_for("embed"), "input": texts}
            )
            response.raise_for_status()
        except Exception as exc:
            await record_llm_call(
                role="embed",
                model=self.model_for("embed"),
                kind="embed",
                duration_ms=int((time.monotonic() - start) * 1000),
                request={"batch_size": len(texts)},
                error=str(exc),
            )
            raise
        # Batch size only — 300+ full payloads a night would drown the log.
        await record_llm_call(
            role="embed",
            model=self.model_for("embed"),
            kind="embed",
            duration_ms=int((time.monotonic() - start) * 1000),
            request={"batch_size": len(texts)},
        )
        rows = sorted(response.json()["data"], key=lambda r: r["index"])
        return [_truncate_normalize(row["embedding"]) for row in rows]


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
