"""LLM gateway — the single module through which all model access flows.

Code asks for a *role* (`writer`, `fast`, `embed`); config maps each role to a
model name on an OpenAI-compatible endpoint (spec §7). Structured output is
enforced twice: the JSON schema is sent as a `response_format` so llama.cpp
constrains generation grammatically, and the response is validated with the
same pydantic model client-side, with repair-prompt retries.
"""

import logging
import math
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import settings
from ..models import EMBEDDING_DIM

log = logging.getLogger("episteme.llm")

Role = Literal["writer", "fast", "embed"]

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMGateway:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            transport=transport,
        )

    def model_for(self, role: Role) -> str:
        model = {
            "writer": settings.llm_model_writer,
            "fast": settings.llm_model_fast,
            "embed": settings.llm_model_embed,
        }[role]
        if not model:
            raise LLMError(f"No model configured for role {role!r} (see LLM_MODEL_* env)")
        return model

    async def is_available(self) -> bool:
        try:
            response = await self._client.get("/models", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def chat(
        self,
        role: Role,
        system: str,
        user: str,
        response_schema: dict | None = None,
        temperature: float = 0.3,
    ) -> str:
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
        response = await self._client.post("/chat/completions", json=payload)
        if response.status_code == 400 and response_schema is not None:
            # Older llama.cpp builds use the pre-OpenAI "json_object" + schema form.
            payload["response_format"] = {"type": "json_object", "schema": response_schema}
            response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        log.info(
            "%s completion: %s prompt + %s completion tokens",
            role,
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        return data["choices"][0]["message"]["content"]

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
        response = await self._client.post(
            "/embeddings", json={"model": self.model_for("embed"), "input": texts}
        )
        response.raise_for_status()
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
