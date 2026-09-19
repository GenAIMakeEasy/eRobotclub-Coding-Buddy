"""Text-to-speech for the assistant's own non-code turns (a clarifying
question, a plan-confirmation message — `kind="ask"` in the Build protocol,
never a phase/complete turn that carries blocks) via ElevenLabs' REST API.

UNVERIFIED LIVE as of writing: every other backend integration in this
codebase (Gemini, OpenRouter) was built only after confirming its real
behavior against a live key (see their own module docstrings) — that
discipline could not be followed here, because no working ElevenLabs
secret key has been available. A key pasted earlier this project turned
out to be the dashboard's "key ID", not the actual secret (which starts
`sk_` and is shown only once, at creation/rotation). This client is written
to ElevenLabs' documented REST shape as of this writing, but the exact
request/response shape, error format, and any account-tier gotchas (e.g. a
free tier's character quota, or per-voice availability) are NOT confirmed
live the way the rest of this codebase's backends are — verify against a
real `sk_...` key before trusting this in front of a classroom, the same
"research first" pass every other client here already had.

The STT half of "voice for this app" (speech input via ElevenLabs'
Speech-to-Text) is a separate, still-unbuilt feature blocked on the same
missing real key — this module only covers TTS (voice OUT).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from app.llm.ollama_client import ModelUnavailable


@dataclass
class TTSResult:
    audio_bytes: bytes
    content_type: str  # e.g. "audio/mpeg"


@dataclass
class ElevenLabsTTS:
    api_key: Optional[str]
    voice_id: str
    model_id: str
    timeout_seconds: float = 30.0
    transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject httpx.MockTransport

    async def synthesize(self, text: str) -> TTSResult:
        if not self.api_key:
            raise ModelUnavailable("ELEVENLABS_API_KEY is not set")
        if not text.strip():
            raise ModelUnavailable("no text to speak")

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                resp = await client.post(
                    url,
                    headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
                    json={"text": text, "model_id": self.model_id},
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # ElevenLabs returns a JSON error body even on failure -- surface
            # it if present, rather than just the bare status code, so a
            # wrong-key-type mistake (the exact failure mode already hit
            # once with a "key ID instead of key" paste) is diagnosable
            # from the error message alone.
            detail = exc.response.text[:300]
            raise ModelUnavailable(f"ElevenLabs TTS request failed ({exc.response.status_code}): {detail}") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"ElevenLabs TTS request failed: {exc}") from exc

        content_type = resp.headers.get("content-type", "audio/mpeg")
        if not resp.content:
            raise ModelUnavailable("ElevenLabs TTS returned no audio")
        return TTSResult(audio_bytes=resp.content, content_type=content_type)
