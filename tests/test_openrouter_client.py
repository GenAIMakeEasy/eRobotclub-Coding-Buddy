"""OpenRouterClient implements the same ChatClient protocol as
OllamaClient/GeminiClient, translating our shapes into OpenRouter's
OpenAI-compatible chat completions format. httpx.MockTransport stands in
for the network."""

import asyncio
import json

import httpx
import pytest

from app.llm.ollama_client import ModelUnavailable
from app.llm.openrouter_client import OpenRouterClient

MINIMAL_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def run(coro):
    return asyncio.run(coro)


def client_with(response_json: dict, status_code: int = 200, api_key: str = "test-key") -> OpenRouterClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_json)

    return OpenRouterClient(api_key=api_key, model="or-test", transport=httpx.MockTransport(handler))


def choice_response(content: str, model: str = "or-test") -> dict:
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}


def test_a_missing_api_key_raises_immediately():
    client = OpenRouterClient(api_key="")
    with pytest.raises(ModelUnavailable, match="OPENROUTER_API_KEY"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_content_is_extracted_from_the_first_choice():
    client = client_with(choice_response('{"summary": "ok"}'))
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert result.text == '{"summary": "ok"}'


def test_the_actual_resolved_model_is_reported_not_just_the_requested_one():
    # "openrouter/free" is an auto-router -- the response's own `model`
    # field says which model actually answered, which is more useful to
    # log than the alias that was requested.
    client = client_with(choice_response("{}", model="google/gemma-4-26b-a4b-it"))
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert result.model == "google/gemma-4-26b-a4b-it"


def test_the_api_key_goes_in_the_authorization_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=choice_response("{}"))

    client = OpenRouterClient(api_key="secret-123", model="or-test", transport=httpx.MockTransport(handler))
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert captured["headers"]["authorization"] == "Bearer secret-123"


def test_schema_is_sent_as_a_strict_json_schema_response_format():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response("{}"))

    client = client_with(choice_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    fmt = captured["payload"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == MINIMAL_SCHEMA


def test_provider_is_sorted_by_throughput_and_requires_real_parameter_support():
    # Caught live: `sort: throughput` removed once while chasing an
    # unrelated bug (proved not the cause), then left removed by oversight
    # -- requests got dramatically slower in the meantime (707s/654s
    # logged, vs. ~280s worst case observed with this set). Locking the
    # setting in so it can't silently disappear again the same way.
    #
    # `require_parameters` added separately: even with throughput-sort
    # restored, real responses kept coming back as pure conversational
    # prose -- nothing JSON-shaped anywhere in them, `strict: true`
    # entirely ignored. Sorting for speed alone says nothing about which
    # candidate providers actually support structured output; this
    # filters the routing pool to only ones that do.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response("{}"))

    client = client_with(choice_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert captured["payload"]["provider"] == {"sort": "throughput", "require_parameters": True}


def test_system_and_history_roles_are_sent_as_is_not_remapped():
    # Unlike Gemini (which needs assistant -> model), OpenAI-compatible
    # APIs use the same role names this codebase already uses throughout.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response("{}"))

    client = client_with(choice_response("{}"))
    client.transport = httpx.MockTransport(handler)
    history = [
        {"role": "user", "content": "make it follow a line"},
        {"role": "assistant", "content": "which sensor?"},
    ]
    run(client.chat(system="you are a robot tutor", user="No.1", schema=MINIMAL_SCHEMA, history=history))

    messages = captured["payload"]["messages"]
    assert messages[0] == {"role": "system", "content": "you are a robot tutor"}
    assert messages[1] == {"role": "user", "content": "make it follow a line"}
    assert messages[2] == {"role": "assistant", "content": "which sensor?"}
    assert messages[3] == {"role": "user", "content": "No.1"}


def test_an_image_is_sent_as_an_image_url_data_uri_on_the_final_user_turn():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response("{}"))

    client = client_with(choice_response("{}"))
    client.transport = httpx.MockTransport(handler)
    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA, image_base64="ZmFrZQ=="))

    last = captured["payload"]["messages"][-1]
    assert last["content"][0] == {"type": "text", "text": "u"}
    assert last["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}


def test_an_image_attached_call_routes_to_the_vision_model_not_the_text_model():
    # Caught live: OPENROUTER_MODEL can legitimately be a text-only model
    # (qwen3-30b-a3b-instruct-2507 is), which OpenRouter 404s outright for
    # any image-attached request -- "No endpoints found that support image
    # input". `model`/`vision_model` are deliberately separate knobs; only
    # an image-bearing call should ever pay the vision-model switch.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=choice_response("{}"))

    client = OpenRouterClient(
        api_key="test-key", model="text-only-model", vision_model="vision-model",
        transport=httpx.MockTransport(handler),
    )

    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert captured["payload"]["model"] == "text-only-model"

    run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA, image_base64="ZmFrZQ=="))
    assert captured["payload"]["model"] == "vision-model"


def test_no_choices_raises_model_unavailable():
    client = client_with({"choices": []})
    with pytest.raises(ModelUnavailable, match="no choices"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_an_error_body_raises_model_unavailable_even_with_a_200_status():
    # OpenRouter can return {"error": {...}} inside a 200 response body,
    # not just as an HTTP error status.
    client = client_with({"error": {"message": "model overloaded"}})
    with pytest.raises(ModelUnavailable, match="OpenRouter returned an error"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_null_content_raises_model_unavailable():
    # Observed live with openai/gpt-oss-20b:free: a reasoning model can
    # burn its whole budget in an unparsed `reasoning` field and leave
    # `content` null.
    client = client_with({"choices": [{"message": {"role": "assistant", "content": None, "reasoning": "..."}}]})
    with pytest.raises(ModelUnavailable, match="no content"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_an_http_error_status_raises_model_unavailable():
    client = client_with({"error": "bad request"}, status_code=400)
    with pytest.raises(ModelUnavailable, match="OpenRouter request failed"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_a_request_that_never_goes_fully_silent_is_still_cut_off():
    # Caught live: a real request ran well past 10 minutes with no timeout
    # ever firing and no error ever logged. httpx's own `timeout=` is a
    # PER-OPERATION limit (max gap between chunks arriving), not a hard
    # overall deadline -- a connection trickling data slowly enough to
    # never go fully silent for the whole window can run forever even
    # with a "timeout" configured. asyncio.wait_for() must be a genuine
    # wall-clock backstop regardless of what httpx's own timeout does or
    # doesn't catch -- simulated here with a handler that simply never
    # returns within the client's configured timeout.
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)  # far longer than the client's own timeout below
        return httpx.Response(200, json=choice_response("{}"))

    client = OpenRouterClient(
        api_key="test-key", model="or-test", timeout_seconds=0.05, transport=httpx.MockTransport(handler)
    )
    # `str(asyncio.TimeoutError())` is an empty string -- caught live, a
    # real timeout logged as a bare "OpenRouter request failed: " with
    # nothing after the colon, no way to tell a timeout apart from any
    # other failure after the fact. Must say "timed out" explicitly.
    with pytest.raises(ModelUnavailable, match="timed out after 0.05s"):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
