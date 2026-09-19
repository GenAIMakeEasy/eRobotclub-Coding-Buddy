"""OllamaClient's job is translating Ollama's HTTP quirks into a clean
ChatResult or a raised ModelUnavailable. Two quirks were found live against a
real qwen3-vl:8b: a reasoning model can burn its whole output budget
"thinking" and never reach `content` (must raise), and even with thinking
disabled, a complete JSON answer can still land in `thinking` instead of
`content` (must be recovered, but only when generation finished cleanly).

httpx.MockTransport (built into httpx, no extra dependency) stands in for
the network so these run without a live Ollama."""

import asyncio
import json

import httpx
import pytest

from app.llm.ollama_client import ModelUnavailable, OllamaClient

MINIMAL_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def run(coro):
    return asyncio.run(coro)


def client_with(response_json: dict, status_code: int = 200) -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_json)

    return OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))


def test_content_is_returned_directly_when_present():
    client = client_with({"message": {"role": "assistant", "content": '{"summary": "ok"}'}, "done_reason": "stop"})
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert result.text == '{"summary": "ok"}'
    assert result.model == "test-model"


def test_thinking_is_used_as_a_fallback_when_content_is_empty_and_generation_stopped_cleanly():
    payload = '{"summary": "recovered from thinking"}'
    client = client_with({"message": {"role": "assistant", "content": "", "thinking": payload}, "done_reason": "stop"})
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert result.text == payload


def test_empty_content_with_a_length_truncated_thinking_blob_still_raises():
    # The live failure mode this guards against: a reasoning model spends its
    # whole budget deliberating and gets cut off mid-thought, never producing
    # a real answer. That must not be silently accepted as if it were one.
    client = client_with({
        "message": {"role": "assistant", "content": "", "thinking": "still reasoning about which dropdown means..."},
        "done_reason": "length",
    })
    with pytest.raises(ModelUnavailable, match="no content"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_empty_content_and_no_thinking_at_all_raises():
    client = client_with({"message": {"role": "assistant", "content": ""}, "done_reason": "stop"})
    with pytest.raises(ModelUnavailable, match="no content"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_an_http_error_status_raises_model_unavailable():
    client = client_with({"error": "model not found"}, status_code=404)
    with pytest.raises(ModelUnavailable, match="Ollama request failed"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_thinking_is_disabled_and_a_token_cap_is_set_in_the_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})

    client = OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert captured["payload"]["think"] is False
    assert captured["payload"]["options"]["num_predict"] > 0
    assert captured["payload"]["format"] == MINIMAL_SCHEMA


def test_context_length_is_sent_and_defaults_generously():
    # Ollama defaults to 4096 if this is never set. A real request (system
    # prompt + routed directory + line-following strategy doc + a couple of
    # history turns) measured ~3500 input tokens -- default context leaves
    # almost no room for the response, and the model silently truncates into
    # garbled output instead of erroring. Observed live, not hypothetical.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})

    client = OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert captured["payload"]["options"]["num_ctx"] >= 8192


def test_an_image_is_attached_to_the_user_message_only():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})

    client = OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA, image_base64="ZmFrZQ=="))

    messages = captured["payload"]["messages"]
    assert "images" not in messages[0]  # system message
    assert messages[1]["images"] == ["ZmFrZQ=="]


def test_repeat_penalty_is_higher_only_when_an_image_is_attached():
    # Measured live: 1.1 is correct for text-only (1.3 garbles slot values by
    # fighting the JSON grammar's legitimate repeated tokens); 1.1 lets a
    # vision request fall into a genuine repetition loop, 1.3 doesn't. A
    # single global value can't serve both, so it's picked per-request.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("payloads", []).append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})

    client = OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA, image_base64="ZmFrZQ=="))

    text_only, with_image = captured["payloads"]
    assert text_only["options"]["repeat_penalty"] == 1.1
    assert with_image["options"]["repeat_penalty"] == 1.3


def test_warmup_posts_to_generate_with_keep_alive_indefinite():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"done": True})

    client = OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))
    run(client.warmup())

    assert captured["url"] == "http://fake/api/generate"
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["keep_alive"] == -1


def test_warmup_matches_chats_context_length_so_it_never_forces_a_reload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"done": True})

    client = OllamaClient(
        host="http://fake", model="test-model", context_length=8192, transport=httpx.MockTransport(handler)
    )
    run(client.warmup())
    assert captured["payload"]["options"]["num_ctx"] == 8192


def test_no_image_means_no_images_key_at_all():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}, "done_reason": "stop"})

    client = OllamaClient(host="http://fake", model="test-model", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert "images" not in captured["payload"]["messages"][1]
