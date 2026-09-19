"""A thin async wrapper over Ollama's /api/embed, used to turn a skill-doc
or a student's question into a vector for similarity search
(app/skills/retrieve.py).

A separate, small model (nomic-embed-text, ~274MB) from the chat model
(gemma4:12b-it-qat), deliberately — embedding runs on every single Build/
Debug request (the retrieval step), so a purpose-built, much smaller model
keeps that step fast and leaves the 16GB budget for the chat model where it
actually matters.
"""

from __future__ import annotations

from typing import Optional

import httpx

from app import config


class EmbeddingUnavailable(Exception):
    """The embedding backend could not be reached, or returned something
    unusable. Callers (app/skills/retrieve.py) fall back to keyword
    matching rather than failing the whole request over a missing
    "nice-to-have" retrieval step."""


class OllamaEmbedder:
    def __init__(
        self,
        host: str = config.OLLAMA_HOST,
        model: str = config.OLLAMA_EMBED_MODEL,
        timeout_seconds: float = 20.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.host = host
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport  # tests inject httpx.MockTransport here

    async def embed(self, text: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    f"{self.host}/api/embed", json={"model": self.model, "input": text}
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"embedding request failed: {exc}") from exc

        data = response.json()
        embeddings = data.get("embeddings")
        if not embeddings or not embeddings[0]:
            raise EmbeddingUnavailable("embedding response had no vector")
        return embeddings[0]
