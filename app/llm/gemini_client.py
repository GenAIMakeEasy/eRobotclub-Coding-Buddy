"""A ChatClient backed by Google's Gemini API — the cloud fallback the
design always kept open (Q7: "local is primary, not sacred"). Implements the
exact same protocol as OllamaClient, so the pipeline never knows which one
it's talking to.

Two things verified live against the real API before writing this, not
assumed (see docs/adr/0004-swappable-model-backend.md):

1. Gemini's `responseSchema` field is an OpenAPI-3.0 subset with NO support
   for `$defs`/`$ref` — it flatly rejects our recursive ProgramBlock schema.
   `responseJsonSchema` is a different field that accepts real JSON Schema,
   `$ref` included, and was confirmed to handle our exact schema correctly.
2. Gemini 2.5 Flash has thinking on by default (`thoughtsTokenCount` > 0 on
   a trivial request) — the same category of problem qwen3-vl caused
   locally. `thinkingConfig: {thinkingBudget: 0}` disables it; confirmed
   live it drops thoughtsTokenCount entirely and cuts total tokens by ~70%
   on a trivial request.

Both re-verified live later in a real classroom deployment, against a
FRESH Google account's API key, and both had moved:

- `gemini-2.5-flash` now 404s for any new-account key ("This model ... is
  no longer available to new users") even though it still appears in the
  `/models` listing endpoint — the listing does not mean generateContent
  will actually accept it. `gemini-flash-latest` (an alias, not a pinned
  version) is what's confirmed working now; being an alias, it can move
  under us again without warning, which is the tradeoff for not hand-
  picking a dated version that dead-ends the same way.
- `thinkingBudget: 0` is REJECTED ("Request contains an invalid argument")
  by whatever model `gemini-flash-latest` currently resolves to
  (`gemini-3.6-flash` at last check) — 0 used to be a valid "off" value on
  2.5 Flash and apparently no longer is on newer models. `1` (the smallest
  non-zero budget) was confirmed live to be accepted and to behave the
  same as `0` did: no `thoughtsTokenCount`, same low total token count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from app import config
from app.llm.ollama_client import ChatResult, ModelUnavailable


@dataclass
class GeminiClient:
    api_key: Optional[str] = None
    model: str = config.GEMINI_MODEL
    timeout_seconds: float = config.GEMINI_TIMEOUT_SECONDS
    transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject httpx.MockTransport

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = config.GEMINI_API_KEY

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
            raise ModelUnavailable("GEMINI_API_KEY is not set")

        contents = []
        for turn in history or []:
            # Gemini uses "model" where we use "assistant" everywhere else.
            role = "model" if turn["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": turn["content"]}]})

        user_parts: list[dict] = [{"text": user}]
        if image_base64:
            user_parts.append({"inline_data": {"mime_type": "image/png", "data": image_base64}})
        contents.append({"role": "user", "parts": user_parts})

        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "thinkingConfig": {"thinkingBudget": 1},
                "temperature": temperature,
            },
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    url, json=payload, headers={"x-goog-api-key": self.api_key}
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"Gemini request failed: {exc}") from exc

        data = response.json()

        block_reason = data.get("promptFeedback", {}).get("blockReason")
        if block_reason:
            raise ModelUnavailable(f"Gemini blocked the request: {block_reason}")

        candidates = data.get("candidates") or []
        if not candidates:
            raise ModelUnavailable(f"Gemini returned no candidates: {data!r}")

        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            raise ModelUnavailable(f"Gemini returned no text: {data!r}")

        return ChatResult(text=text, model=self.model)
