"""GeminiClient implements the same ChatClient protocol as OllamaClient (see
docs/adr/0004-swappable-model-backend.md), translating our shapes into
Gemini's. httpx.MockTransport stands in for the network."""

import asyncio
import json

import httpx
import pytest

from app.llm.gemini_client import GeminiClient
from app.llm.ollama_client import ModelUnavailable

MINIMAL_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def run(coro):
    return asyncio.run(coro)


def client_with(response_json: dict, status_code: int = 200, api_key: str = "test-key") -> GeminiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_json)

    return GeminiClient(api_key=api_key, model="gemini-test", transport=httpx.MockTransport(handler))


def candidate_response(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}]}


def test_a_missing_api_key_raises_immediately():
    # api_key="" (not None) so __post_init__'s fallback-to-config doesn't
    # mask this with whatever key happens to be configured in this
    # environment's .env.
    client = GeminiClient(api_key="")
    with pytest.raises(ModelUnavailable, match="GEMINI_API_KEY"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_content_is_extracted_from_the_first_candidate():
    client = client_with(candidate_response('{"summary": "ok"}'))
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert result.text == '{"summary": "ok"}'
    assert result.model == "gemini-test"


def test_the_api_key_goes_in_a_header_not_the_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=candidate_response("{}"))

    client = GeminiClient(api_key="secret-123", model="gemini-test", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert "secret-123" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "secret-123"


def test_schema_is_sent_as_response_json_schema_not_response_schema():
    # The field that actually supports our recursive ProgramBlock type --
    # verified live against the real API before writing this client.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=candidate_response("{}"))

    client = client_with(candidate_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    gen_config = captured["payload"]["generationConfig"]
    assert gen_config["responseJsonSchema"] == MINIMAL_SCHEMA
    assert "responseSchema" not in gen_config


def test_thinking_is_disabled():
    # thinkingBudget 0 used to be the "off" value on gemini-2.5-flash, but
    # a later model (gemini-flash-latest resolving to gemini-3.6-flash)
    # rejected 0 outright as an invalid argument -- 1 (smallest non-zero
    # budget) is what's confirmed live to behave the same as 0 did.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=candidate_response("{}"))

    client = client_with(candidate_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert captured["payload"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1


def test_history_roles_map_assistant_to_model():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=candidate_response("{}"))

    client = client_with(candidate_response("{}"))
    client.transport = httpx.MockTransport(handler)
    history = [
        {"role": "user", "content": "make it follow a line"},
        {"role": "assistant", "content": "which sensor?"},
    ]
    run(client.chat(system="s", user="No.1", schema=MINIMAL_SCHEMA, history=history))

    contents = captured["payload"]["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "make it follow a line"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "which sensor?"}]}
    assert contents[2] == {"role": "user", "parts": [{"text": "No.1"}]}


def test_an_image_is_sent_as_inline_data_on_the_final_user_turn():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=candidate_response("{}"))

    client = client_with(candidate_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA, image_base64="ZmFrZQ=="))

    last_turn = captured["payload"]["contents"][-1]
    assert last_turn["parts"][0] == {"text": "u"}
    assert last_turn["parts"][1] == {"inline_data": {"mime_type": "image/png", "data": "ZmFrZQ=="}}


def test_a_blocked_prompt_raises_model_unavailable():
    client = client_with({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ModelUnavailable, match="blocked"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_no_candidates_raises_model_unavailable():
    client = client_with({"candidates": []})
    with pytest.raises(ModelUnavailable, match="no candidates"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_an_http_error_status_raises_model_unavailable():
    client = client_with({"error": "bad request"}, status_code=400)
    with pytest.raises(ModelUnavailable, match="Gemini request failed"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_system_instruction_is_sent_separately_from_contents():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=candidate_response("{}"))

    client = client_with(candidate_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="you are a helpful robot tutor", user="u", schema=MINIMAL_SCHEMA))

    assert captured["payload"]["systemInstruction"]["parts"][0]["text"] == "you are a helpful robot tutor"
    assert all("helpful robot tutor" not in json.dumps(c) for c in captured["payload"]["contents"])
