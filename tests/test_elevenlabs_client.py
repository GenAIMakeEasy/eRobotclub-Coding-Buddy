"""ElevenLabsTTS: request/response shape, error handling. httpx.MockTransport
stands in for the network -- see this client's own module docstring for
what's NOT verified here (a real ElevenLabs key was never available while
building it, unlike every other backend client in this codebase)."""

import asyncio

import httpx
import pytest

from app.llm.ollama_client import ModelUnavailable
from app.tts.elevenlabs_client import ElevenLabsTTS


def run(coro):
    return asyncio.run(coro)


def client_with(status_code=200, content=b"fake-mp3-bytes", content_type="audio/mpeg", api_key="test-key"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content, headers={"content-type": content_type})

    return ElevenLabsTTS(
        api_key=api_key, voice_id="voice-1", model_id="model-1", transport=httpx.MockTransport(handler)
    )


def test_a_missing_api_key_raises_immediately():
    client = ElevenLabsTTS(api_key=None, voice_id="v", model_id="m")
    with pytest.raises(ModelUnavailable, match="ELEVENLABS_API_KEY"):
        run(client.synthesize("hello"))


def test_empty_text_raises_without_a_network_call():
    client = ElevenLabsTTS(api_key="k", voice_id="v", model_id="m")
    with pytest.raises(ModelUnavailable, match="no text"):
        run(client.synthesize("   "))


def test_a_successful_response_returns_the_audio_bytes():
    client = client_with(content=b"\xff\xfb\x90audio-data")
    result = run(client.synthesize("hello there"))
    assert result.audio_bytes == b"\xff\xfb\x90audio-data"
    assert result.content_type == "audio/mpeg"


def test_the_api_key_goes_in_the_xi_api_key_header_not_authorization():
    # ElevenLabs' own auth convention -- distinct from every other client
    # in this codebase, which use `Authorization: Bearer`.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"data", headers={"content-type": "audio/mpeg"})

    client = ElevenLabsTTS(api_key="the-real-key", voice_id="v", model_id="m", transport=httpx.MockTransport(handler))
    run(client.synthesize("hi"))
    assert captured["headers"]["xi-api-key"] == "the-real-key"


def test_the_voice_id_is_in_the_url_path():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b"data", headers={"content-type": "audio/mpeg"})

    client = ElevenLabsTTS(
        api_key="k", voice_id="my-voice-id", model_id="m", transport=httpx.MockTransport(handler)
    )
    run(client.synthesize("hi"))
    assert "my-voice-id" in captured["url"]


def test_a_401_response_raises_with_the_body_surfaced():
    # Caught once already, for a different key (a "key ID" pasted where the
    # real secret belonged) -- surfacing the response body, not just the
    # status code, is what makes that failure mode diagnosable at all.
    client = client_with(status_code=401, content=b'{"detail": "invalid api key"}', content_type="application/json")
    with pytest.raises(ModelUnavailable, match="invalid api key"):
        run(client.synthesize("hi"))


def test_a_response_with_no_audio_bytes_raises():
    client = client_with(content=b"")
    with pytest.raises(ModelUnavailable, match="no audio"):
        run(client.synthesize("hi"))
