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


def test_a_failed_primary_model_falls_back_and_flags_the_result():
    # The whole point: a student's turn should still succeed, quietly, when
    # only the primary model is down -- not surface as a flat error just
    # because ONE of two configured models happened to be unavailable.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["model"])
        if payload["model"] == "primary-model":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=choice_response("{}", model="fallback-model"))

    client = OpenRouterClient(
        api_key="test-key", model="primary-model", fallback_model="fallback-model",
        transport=httpx.MockTransport(handler),
    )
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    assert calls == ["primary-model", "fallback-model"]
    assert result.model == "fallback-model"
    assert result.used_fallback is True
    assert result.fallback_reason  # non-empty -- the original failure's own message


def test_fallback_is_never_used_for_an_image_attached_call():
    # fallback_model isn't guaranteed to be vision-capable -- an image
    # call already routes to vision_model, and a text-only fallback for
    # THAT would just trade one wrong model for another.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["model"])
        return httpx.Response(429, json={"error": "rate limited"})

    client = OpenRouterClient(
        api_key="test-key", model="primary-model", vision_model="vision-model",
        fallback_model="fallback-model", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelUnavailable):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA, image_base64="ZmFrZQ=="))

    assert calls == ["vision-model"]


def test_when_both_primary_and_fallback_fail_the_original_error_wins():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        detail = "primary down" if payload["model"] == "primary-model" else "fallback down too"
        # A 200 with an `error` body, not an HTTP error status -- so the
        # distinguishing text in `detail` actually survives into the raised
        # message rather than being replaced by httpx's own generic
        # "Server error '500 ...'" text.
        return httpx.Response(200, json={"error": detail})

    client = OpenRouterClient(
        api_key="test-key", model="primary-model", fallback_model="fallback-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelUnavailable) as exc_info:
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))

    message = str(exc_info.value)
    assert "primary down" in message
    assert "fallback-model" in message
    assert "fallback down too" in message


def test_no_fallback_configured_raises_the_original_error_immediately():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    client = OpenRouterClient(
        api_key="test-key", model="primary-model", fallback_model=None,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelUnavailable):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))


def test_a_404_no_endpoints_retries_once_without_require_parameters():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["provider"])
        if payload["provider"].get("require_parameters"):
            return httpx.Response(404, json={"error": {"message": "No endpoints found that can handle the requested parameters.", "code": 404}})
        return httpx.Response(200, json=choice_response("{}"))

    client = OpenRouterClient(api_key="k", model="or-test", fallback_model=None, transport=httpx.MockTransport(handler))
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert result.text == "{}" and not result.used_fallback
    assert seen == [{"sort": "throughput", "require_parameters": True}, {"sort": "throughput"}]


def test_other_404s_are_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    client = OpenRouterClient(api_key="k", model="or-test", fallback_model=None, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelUnavailable):
        run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert len(calls) == 1


def test_openai_models_get_a_closed_schema_and_others_keep_the_original():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "object", "properties": {"c": {"type": "integer", "default": 1}}}}, "required": ["a"]}
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=choice_response("{}"))

    for model in ("openai/gpt-6-luna", "z-ai/glm-5.3-flash"):
        client = OpenRouterClient(api_key="k", model=model, fallback_model=None, transport=httpx.MockTransport(handler))
        run(client.chat(system="s", user="u", schema=schema))
    openai_schema, other_schema = (p["response_format"]["json_schema"]["schema"] for p in sent)
    assert openai_schema["additionalProperties"] is False and openai_schema["required"] == ["a", "b"]
    assert openai_schema["properties"]["b"]["additionalProperties"] is False
    assert "default" not in openai_schema["properties"]["b"]["properties"]["c"]
    assert other_schema == schema and "additionalProperties" not in other_schema
    assert "additionalProperties" not in schema  # the app's own schema is never mutated


def test_several_fallback_models_are_tried_in_order():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["model"])
        if payload["model"] in ("primary-model", "fallback-one"):
            return httpx.Response(200, json={"error": "down"})
        return httpx.Response(200, json=choice_response("{}", model="fallback-two"))

    client = OpenRouterClient(
        api_key="k", model="primary-model", fallback_model="fallback-one, fallback-two",
        transport=httpx.MockTransport(handler),
    )
    result = run(client.chat(system="s", user="u", schema=MINIMAL_SCHEMA))
    assert calls == ["primary-model", "fallback-one", "fallback-two"]
    assert result.used_fallback is True and result.model == "fallback-two"


def test_anthropic_models_get_the_schema_as_text_not_as_response_format():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=choice_response("{}"))

    client = OpenRouterClient(api_key="k", model="anthropic/claude-haiku-5.5", fallback_model=None,
                              transport=httpx.MockTransport(handler))
    run(client.chat(system="SYS", user="u", schema=MINIMAL_SCHEMA))
    payload = sent[0]
    assert "response_format" not in payload
    assert payload["messages"][0]["content"].startswith("SYS") and json.dumps(MINIMAL_SCHEMA) in payload["messages"][0]["content"]
