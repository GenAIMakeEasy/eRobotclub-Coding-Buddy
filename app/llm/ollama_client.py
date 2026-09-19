"""A thin async wrapper over Ollama's /api/chat, constrained by a JSON Schema.

Deliberately minimal: one function, one job. The pipeline depends on this
signature, not on Ollama specifically — swapping to a cloud model (design's
stated fallback, Q7: "local is primary, not sacred") means writing another
function with this signature, not touching the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

from app import config


class ModelUnavailable(Exception):
    """The backend could not be reached or returned an error. The queue's
    per-job failure handling turns this into a red-box-style failure for one
    student, not a crash for the whole server."""


@dataclass
class ChatResult:
    text: str
    model: str
    # Set by OpenRouterClient when the configured primary model failed and a
    # second call to OPENROUTER_FALLBACK_MODEL succeeded in its place --
    # every other client leaves these at their defaults, since only
    # OpenRouterClient currently has a fallback model to switch to.
    used_fallback: bool = False
    fallback_reason: Optional[str] = None


class ChatClient(Protocol):
    """What the pipeline needs from a model backend. `OllamaClient` below is
    the real implementation; tests substitute a fake with this shape.

    `history` is prior turns of an in-progress Build conversation (the
    vendored skill's ask -> confirm -> phase-by-phase protocol) — a list of
    {"role": "user"|"assistant", "content": str}, oldest first, NOT
    including the system prompt or the new `user` message. Empty/None for a
    fresh question, or for Debug (which never continues a thread)."""

    async def chat(
        self,
        system: str,
        user: str,
        schema: dict,
        image_base64: Optional[str] = None,
        history: Optional[list[dict]] = None,
    ) -> ChatResult: ...


class OllamaClient:
    def __init__(
        self,
        host: str = config.OLLAMA_HOST,
        model: str = config.OLLAMA_MODEL,
        timeout_seconds: float = config.OLLAMA_TIMEOUT_SECONDS,
        context_length: int = config.OLLAMA_CONTEXT_LENGTH,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.host = host
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.context_length = context_length
        self.transport = transport  # tests inject httpx.MockTransport here

    async def chat(
        self,
        system: str,
        user: str,
        schema: dict,
        image_base64: Optional[str] = None,
        history: Optional[list[dict]] = None,
        temperature: float = 0,
    ) -> ChatResult:
        user_message: dict = {"role": "user", "content": user}
        if image_base64:
            user_message["images"] = [image_base64]

        # repeat_penalty needed by mode, not one global value — measured live,
        # both directions:
        #   - text-only + 1.1 (near Ollama's own default): 3/3 clean runs.
        #   - text-only + 1.3: regressed — schema-constrained JSON legitimately
        #     repeats structural tokens (}, ], "kind", digits) constantly, and
        #     a strong penalty fights the grammar, producing garbled slot text.
        #   - image attached + 1.1: fell into a genuine repetition loop
        #     (dozens of duplicate blocks), never stopped until the token cap
        #     cut it off mid-structure.
        #   - image attached + 1.3: no loop, clean stop.
        # So the penalty only goes up when an image is actually attached,
        # which is where the loop was actually observed — the proven-solid
        # text path is left alone rather than paying for a fix it doesn't need.
        repeat_penalty = 1.3 if image_base64 else 1.1

        messages = [{"role": "system", "content": system}, *(history or []), user_message]

        payload = {
            "model": self.model,
            "messages": messages,
            "format": schema,
            "stream": False,
            # Reasoning models (qwen3-vl declares "thinking") will otherwise
            # spend the whole generation budget deliberating in a separate
            # `thinking` field and never reach `content` — observed live: a
            # 133s request hit its length limit mid-thought with content="".
            # Schema-constrained JSON doesn't benefit from chain-of-thought
            # the way free text does, so it's switched off outright.
            "think": False,
            "options": {
                "temperature": temperature,
                # Raised from 2048: a real live run of the full IR3-6
                # line-tracing cascade got cut off mid-string at ~1100-1500
                # output tokens once "Logical And / Or" conditions were
                # correctly nested (each steering branch now carries two
                # extra IR-sensor blocks instead of flattened slots) —
                # legitimately more JSON than before, not a regression.
                "num_predict": 4096,
                "repeat_penalty": repeat_penalty,
                "num_ctx": self.context_length,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(f"{self.host}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"Ollama request failed: {exc}") from exc

        data = response.json()
        message = data.get("message", {})
        content = message.get("content")

        if not content:
            # Observed live with qwen3-vl:8b: even with think=False, the
            # schema-constrained JSON sometimes arrives in `thinking` instead
            # of `content` (an Ollama/model quirk, not a spec). Only trust it
            # when the generation completed cleanly — a `length`-truncated
            # thinking blob is the OTHER failure mode this guards against,
            # and must still raise.
            thinking = message.get("thinking")
            if thinking and data.get("done_reason") == "stop":
                content = thinking
            else:
                raise ModelUnavailable(f"Ollama returned no content: {data!r}")

        return ChatResult(text=content, model=self.model)

    async def warmup(self) -> None:
        """Load the model into memory now, not on the first student's
        question. Without this, Ollama lazily loads on first call and
        unloads after its idle timeout — whoever asks first pays the cold
        load. `keep_alive: -1` keeps it resident indefinitely, appropriate
        for a machine dedicated to this app during class. `num_ctx` must
        match what `chat()` requests, or the first real question forces a
        reload with a different context size — paying exactly the cold-load
        cost this method exists to avoid."""
        payload = {
            "model": self.model,
            "prompt": "",
            "keep_alive": -1,
            "options": {"num_ctx": self.context_length},
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(f"{self.host}/api/generate", json=payload)
            response.raise_for_status()
