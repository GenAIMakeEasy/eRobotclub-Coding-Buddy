"""OllamaEmbedder's job is translating /api/embed's response into a plain
vector or a raised EmbeddingUnavailable — mirrors test_ollama_client.py's
approach: httpx.MockTransport stands in for the network."""

import asyncio

import httpx
import pytest

from app.rag.embeddings import EmbeddingUnavailable, OllamaEmbedder


def run(coro):
    return asyncio.run(coro)


def embedder_with(response_json: dict, status_code: int = 200) -> OllamaEmbedder:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_json)

    return OllamaEmbedder(host="http://fake", model="test-embed", transport=httpx.MockTransport(handler))


def test_a_vector_is_returned_directly():
    embedder = embedder_with({"embeddings": [[0.1, 0.2, 0.3]]})
    vec = run(embedder.embed("hello"))
    assert vec == [0.1, 0.2, 0.3]


def test_an_http_error_raises_embedding_unavailable():
    embedder = embedder_with({"error": "model not found"}, status_code=500)
    with pytest.raises(EmbeddingUnavailable):
        run(embedder.embed("hello"))


def test_a_missing_vector_raises_embedding_unavailable():
    embedder = embedder_with({"embeddings": []})
    with pytest.raises(EmbeddingUnavailable):
        run(embedder.embed("hello"))


def test_a_connection_failure_raises_embedding_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    embedder = OllamaEmbedder(host="http://fake", model="test-embed", transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingUnavailable):
        run(embedder.embed("hello"))
