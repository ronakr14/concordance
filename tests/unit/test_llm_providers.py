"""Provider clients against recorded responses. No socket is ever opened.

`httpx.MockTransport` is the right seam for this: the client's own request
construction, status handling, JSON parsing and error mapping all run for real,
and only the wire is replaced. Mocking `complete` instead would test nothing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from concordance.llm.errors import AuthFailed, InvalidRequest, RateLimited, Transient
from concordance.llm.pricing import ModelPrice, PriceTable
from concordance.llm.providers.groq import GroqProvider
from concordance.llm.providers.openrouter import OpenRouterProvider
from concordance.llm.types import user

pytestmark = pytest.mark.unit


def completion(content: str = '{"ok": true}', model: str = "openai/gpt-oss-20b") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
    }


def transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def responder(status: int, payload: dict[str, Any], headers: dict[str, str] | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload, headers=headers or {})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


# -- the happy path --------------------------------------------------------


def test_groq_normalizes_a_completion() -> None:
    handler = responder(200, completion())
    provider = GroqProvider("key", client=transport(handler))
    response = provider.complete([user("hello")])
    assert response.provider == "groq"
    assert response.model == "openai/gpt-oss-20b"
    assert response.content == '{"ok": true}'
    assert response.prompt_tokens == 120
    assert response.completion_tokens == 40
    assert response.total_tokens == 160
    assert response.finish_reason == "stop"
    assert response.raw["id"] == "chatcmpl-1"


def test_request_targets_the_right_url_and_carries_the_key() -> None:
    handler = responder(200, completion())
    provider = GroqProvider("secret-key", client=transport(handler))
    provider.complete([user("hello")])
    request = handler.seen[0]  # type: ignore[attr-defined]
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"


def test_openrouter_sends_its_attribution_headers() -> None:
    handler = responder(200, completion(model="poolside/laguna-s-2.1:free"))
    provider = OpenRouterProvider("key", client=transport(handler))
    provider.complete([user("hello")])
    request = handler.seen[0]  # type: ignore[attr-defined]
    assert str(request.url).startswith("https://openrouter.ai/api/v1/")
    assert request.headers["x-title"] == "Concordance"


def test_json_mode_is_requested_but_schema_decoding_is_not() -> None:
    """PLAN 11.6: the prompt-based path stays the tested path."""
    handler = responder(200, completion())
    provider = GroqProvider("key", client=transport(handler))
    body = provider.payload([user("hello")], {"type": "object"})
    assert body["response_format"] == {"type": "json_object"}
    assert provider.supports_structured_output is True


def test_schema_decoding_is_reachable_only_when_asked_for_explicitly() -> None:
    provider = GroqProvider("key")
    body = provider.payload([user("hi")], {"type": "object"}, use_schema=True)
    assert body["response_format"]["type"] == "json_schema"


def test_cost_comes_from_the_price_table() -> None:
    prices = PriceTable({"priced-model": ModelPrice(10.0, 20.0, 8_192)})
    handler = responder(200, completion(model="priced-model"))
    provider = GroqProvider("key", model="priced-model", prices=prices, client=transport(handler))
    response = provider.complete([user("hello")])
    # 120 prompt tokens at $10/M plus 40 completion tokens at $20/M.
    assert response.cost_usd == pytest.approx((120 * 10.0 + 40 * 20.0) / 1_000_000)


def test_a_free_model_costs_nothing() -> None:
    handler = responder(200, completion())
    provider = GroqProvider("key", client=transport(handler))
    assert provider.complete([user("hello")]).cost_usd == 0.0


# -- error mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthFailed),
        (403, AuthFailed),
        (429, RateLimited),
        (408, Transient),
        (500, Transient),
        (502, Transient),
        (503, Transient),
        (400, InvalidRequest),
        (404, InvalidRequest),
        (422, InvalidRequest),
    ],
)
def test_status_codes_map_onto_the_taxonomy(status: int, expected: type[Exception]) -> None:
    handler = responder(status, {"error": {"message": "nope"}})
    provider = GroqProvider("key", client=transport(handler))
    with pytest.raises(expected):
        provider.complete([user("hello")])


def test_rate_limit_carries_retry_after() -> None:
    handler = responder(429, {"error": {"message": "slow down"}}, {"retry-after": "7.5"})
    provider = GroqProvider("key", client=transport(handler))
    with pytest.raises(RateLimited) as caught:
        provider.complete([user("hello")])
    assert caught.value.retry_after == pytest.approx(7.5)


def test_a_200_carrying_an_error_object_is_still_a_failure() -> None:
    """OpenRouter does this when an upstream free model is unavailable."""
    handler = responder(200, {"error": {"message": "upstream is down", "code": 503}})
    provider = OpenRouterProvider("key", client=transport(handler))
    with pytest.raises(Transient):
        provider.complete([user("hello")])


def test_an_empty_choices_array_is_transient() -> None:
    handler = responder(200, {"model": "m", "choices": []})
    provider = GroqProvider("key", client=transport(handler))
    with pytest.raises(Transient):
        provider.complete([user("hello")])


def test_a_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider = GroqProvider("key", client=transport(handler))
    with pytest.raises(Transient):
        provider.complete([user("hello")])


def test_a_connection_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = GroqProvider("key", client=transport(handler))
    with pytest.raises(Transient):
        provider.complete([user("hello")])


def test_a_non_json_body_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    provider = GroqProvider("key", client=transport(handler))
    with pytest.raises(Transient):
        provider.complete([user("hello")])


def test_the_api_key_never_reaches_the_error_message() -> None:
    """Provider error bodies echo request context; the key must not survive into a log."""
    handler = responder(
        401,
        {"error": {"message": "invalid api key", "request": {"authorization": "Bearer sk-secret"}}},
    )
    provider = GroqProvider("sk-secret", client=transport(handler))
    with pytest.raises(AuthFailed) as caught:
        provider.complete([user("hello")])
    assert "sk-secret" not in str(caught.value)
    assert "invalid api key" in str(caught.value)
