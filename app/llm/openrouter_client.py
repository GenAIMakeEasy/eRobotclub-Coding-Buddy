"""A ChatClient backed by OpenRouter's OpenAI-compatible API — a second
cloud fallback alongside Gemini (design's Q7: "local is primary, not
sacred"), added specifically because Gemini's free tier kept rate-limiting
(5 requests/minute) during real classroom use. Implements the exact same
protocol as OllamaClient/GeminiClient, so the pipeline never knows which
one it's talking to.

Verified live before writing this, not assumed:

1. `openai/gpt-oss-20b:free` is disqualified: even with `response_format`
   set, it burns its entire output in an unparsed `reasoning` field and
   returns `content: null` — the exact "thinking model never reaches
   content" failure already ruled out locally with qwen3-vl:8b.
2. `google/gemma-4-26b-a4b-it:free`, WITH the real production system
   prompt (the full R Block Reference, not an abbreviated test prompt),
   correctly handles the hardest case in this codebase's history: a
   compound `Logical And / Or` condition nested inside `if then`, using
   real catalog block names, `else_children` correctly nested as a field
   (not a sibling block) — round-tripped clean through the actual Pydantic
   + structural validators, zero errors. An earlier test WITHOUT the real
   system prompt produced structurally invalid output (invented block
   names, `else_children` as its own top-level "block") — the full block
   catalog in context is load-bearing, not optional.
3. `openrouter/free` (OpenRouter's own auto-router across available free
   models) was tried FIRST as the default, on the theory that an alias
   avoids the dead end `gemini-2.5-flash` hit when pinned and later
   deprecated (see gemini_client.py's docstring). That theory failed live:
   the very next request after a clean test routed to the disqualified
   `openai/gpt-oss-20b:free` above. The auto-router picks per-request, so
   one good result proves nothing about the next call. Pinned to
   `google/gemma-4-26b-a4b-it:free` directly instead (see
   app/config.py's OPENROUTER_MODEL) — the alias-over-pin tradeoff only
   pays off if the alias is actually trustworthy.
4. Free tier: 20 requests/minute, 50/day (1000/day after a one-time $10
   credit purchase, which never expires) — meaningfully looser than
   Gemini's observed 5/minute.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

from app import config
from app.llm.ollama_client import ChatResult, ModelUnavailable


@dataclass
class OpenRouterClient:
    api_key: Optional[str] = None
    model: str = config.OPENROUTER_MODEL
    # A model picked for text/JSON quality is not necessarily multimodal --
    # caught live: OPENROUTER_MODEL (qwen3-30b-a3b-instruct-2507) 404s
    # outright on any image-attached call ("No endpoints found that support
    # image input"), for both a student's Debug photo and the admin
    # skills-draft-from-image endpoint. See config.OPENROUTER_VISION_MODEL's
    # own comment for the full story. Only image-attached calls use this.
    vision_model: str = config.OPENROUTER_VISION_MODEL
    timeout_seconds: float = config.OPENROUTER_TIMEOUT_SECONDS
    transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject httpx.MockTransport

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = config.OPENROUTER_API_KEY

    async def chat(
        self,
        system: str,
        user: str,
        schema: dict,
        image_base64: Optional[str] = None,
        history: Optional[list[dict]] = None,
        temperature: float = 0,
    ) -> ChatResult:
        if not self.api_key:
            raise ModelUnavailable("OPENROUTER_API_KEY is not set")

        messages: list[dict] = [{"role": "system", "content": system}]
        for turn in history or []:
            # Already "assistant"/"user" throughout this codebase — unlike
            # Gemini, OpenAI-compatible APIs use the same role names we do.
            messages.append({"role": turn["role"], "content": turn["content"]})

        model = self.model
        if image_base64:
            model = self.vision_model
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user})

        payload = {
            "model": model,
            "messages": messages,
            # A model like deepseek/deepseek-v4-flash can be hosted by more
            # than one backend provider behind OpenRouter's own routing --
            # left unset, OpenRouter optimises for price/uptime, not
            # tokens/sec. "throughput" asks it to prefer whichever backend
            # is fastest right now instead.
            #
            # Removed once, live, while chasing a DIFFERENT bug (a nesting-
            # inversion validation error) on the theory it might be the
            # cause -- it wasn't; that bug reproduced identically without
            # this setting too. Left removed afterward by oversight, not a
            # deliberate decision, and requests got dramatically slower in
            # the meantime (707s and 654s logged, vs. ~280s worst case
            # observed with this setting in place) -- restoring it.
            #
            # `require_parameters`: caught live, repeatedly, even with this
            # setting in place -- a response that was pure conversational
            # prose (a numbered plan, a chatty "Great, I understand!"),
            # nothing JSON-shaped anywhere in it at all, `strict: true`
            # entirely ignored. `sort: throughput` alone picks for SPEED,
            # not for which candidate providers actually support the
            # parameters this request depends on -- `require_parameters`
            # filters the routing pool down to only providers that
            # genuinely support what's being asked for (here, structured
            # JSON output), instead of letting a fast-but-noncompliant one
            # silently drop `response_format` and fall back to a plain
            # chat completion.
            "provider": {"sort": "throughput", "require_parameters": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": schema},
            },
            "temperature": temperature,
        }

        try:
            # Caught live: a real request ran well past 10 minutes with no
            # timeout ever firing, no error ever logged -- httpx.Timeout
            # (what `timeout=` sets here) is a PER-OPERATION limit (max
            # gap between chunks arriving), not a hard overall deadline.
            # A connection that trickles data slowly enough to never go
            # fully silent for the whole window can run indefinitely even
            # with a "180s timeout" configured. asyncio.wait_for() is a
            # genuine wall-clock backstop on top of it -- this request
            # cannot exceed timeout_seconds total, regardless of what
            # httpx's own timeout semantics do or don't catch.
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await asyncio.wait_for(
                    client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    ),
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
        except asyncio.TimeoutError as exc:
            # Caught live: `str(asyncio.TimeoutError())` is an EMPTY
            # string -- the wall-clock backstop above fired correctly
            # (cut off a request that was genuinely taking too long), but
            # the resulting log entry read as a bare "OpenRouter request
            # failed: " with nothing after the colon, no way to tell a
            # timeout apart from any other failure after the fact.
            raise ModelUnavailable(
                f"OpenRouter request failed: timed out after {self.timeout_seconds}s (no response in time)"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"OpenRouter request failed: {exc}") from exc

        data = response.json()

        if "error" in data:
            raise ModelUnavailable(f"OpenRouter returned an error: {data['error']!r}")

        choices = data.get("choices") or []
        if not choices:
            raise ModelUnavailable(f"OpenRouter returned no choices: {data!r}")

        message = choices[0].get("message") or {}
        text = message.get("content")
        if not text:
            # Observed live with openai/gpt-oss-20b:free: a reasoning model
            # can burn its whole budget in `reasoning` and leave `content`
            # null, same failure mode disabled for local/Gemini elsewhere.
            raise ModelUnavailable(f"OpenRouter returned no content: {data!r}")

        return ChatResult(text=text, model=data.get("model", model))
