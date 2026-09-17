"""The shared client. Groq and OpenRouter are both OpenAI-compatible.

Writing two independent HTTP clients for two endpoints that take the identical
request body would mean two places to get the error mapping wrong. Instead the
wire protocol lives here once and each provider contributes only what actually
differs: base URL, key, extra headers, and how that vendor words its failures.

Error mapping is the substance of this module. A 429 is obvious; the cases that
matter are the ones providers get creative about - OpenRouter returning 200 with
an ``error`` object in the body, Groq returning 400 for a context overflow that
is really our fault. Every one of them lands on the taxonomy in `llm.errors`,
and the router above never sees an ``httpx`` exception.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from concordance.llm.errors import (
    AuthFailed,
    InvalidRequest,
    LLMError,
    RateLimited,
    Transient,
)
from concordance.llm.pricing import PriceTable
from concordance.llm.tls import ssl_context
from concordance.llm.types import ChatMessage, LLMResponse

DEFAULT_TIMEOUT = 30.0
# A reasoning model spends hidden tokens before the first visible character,
# so this is the reasoning budget plus the answer, not the answer alone. Too
# small and the JSON is truncated mid-object: Groq turns that into a 400
# `json_validate_failed` rather than a short completion.
DEFAULT_MAX_TOKENS = 2_000
DEFAULT_TEMPERATURE = 0.0


class OpenAICompatibleProvider:
    """A chat-completions endpoint, bound to one model and one key.

    Not abstract: both concrete providers are this class with different
    constructor defaults, which keeps the two-provider difference to data.
    """

    name = "openai-compatible"
    base_url = ""
    #: Whether this vendor's API accepts a JSON schema for constrained decoding.
    #: Advertised, never used - the router takes the prompt-based path by
    #: policy (PLAN 11.6) so the repair path stays exercised.
    supports_structured_output = False

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        prices: PriceTable | None = None,
        client: httpx.Client | None = None,
        extra_headers: dict[str, str] | None = None,
        ca_bundle: Path | str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.prices = prices or PriceTable.default()
        self.extra_headers = dict(extra_headers or {})
        self.ca_bundle = ca_bundle
        if base_url:
            self.base_url = base_url
        # An injected client is how the tests reach this code without a
        # network: `httpx.MockTransport` in, real behaviour out.
        self._client = client
        self._owns_client = client is None

    # -- wire -------------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            verify = ssl_context(self.ca_bundle)
            self._client = (
                httpx.Client(timeout=self.timeout, verify=verify)
                if verify is not None
                else httpx.Client(timeout=self.timeout)
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

    def payload(
        self, messages: list[ChatMessage], schema: dict[str, Any] | None, **opts: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": opts.get("model", self.model),
            "messages": [m.as_dict() for m in messages],
            "temperature": opts.get("temperature", self.temperature),
            "max_tokens": opts.get("max_tokens", self.max_tokens),
        }
        if opts.get("json_mode", True):
            # `json_object` is not the same as schema-constrained decoding: it
            # asks for syntactically valid JSON and says nothing about shape,
            # so the schema guard downstream is still doing the real work.
            body["response_format"] = {"type": "json_object"}
        if schema is not None and self.supports_structured_output and opts.get("use_schema", False):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "adjudication", "schema": schema, "strict": True},
            }
        return body

    # -- the call ---------------------------------------------------------
    def complete(
        self,
        messages: list[ChatMessage],
        schema: dict[str, Any] | None = None,
        **opts: Any,
    ) -> LLMResponse:
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = self.payload(messages, schema, **opts)
        started = time.perf_counter()
        try:
            response = self.client.post(
                url,
                json=body,
                headers=self.headers(),
                timeout=opts.get("timeout", self.timeout),
            )
        except httpx.TimeoutException as exc:
            raise Transient(f"timeout after {self.timeout}s", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise Transient(f"transport error: {exc}", provider=self.name) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            raise self.map_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise Transient("provider returned a non-JSON body", provider=self.name) from exc
        # OpenRouter answers 200 with an error object when an upstream model is
        # unavailable. Trusting the status code alone would surface that as a
        # parse failure three layers up.
        if isinstance(data.get("error"), dict):
            raise self.map_body_error(data["error"], response.status_code)
        return self.to_response(data, latency_ms)

    # -- normalization ----------------------------------------------------
    def to_response(self, data: dict[str, Any], latency_ms: int) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise Transient("provider returned no choices", provider=self.name)
        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        model = str(data.get("model") or self.model)
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        return LLMResponse(
            content=str(message.get("content") or ""),
            provider=self.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=self.prices.cost(model, prompt_tokens, completion_tokens),
            finish_reason=str(choices[0].get("finish_reason") or ""),
            raw=data,
        )

    # -- errors -----------------------------------------------------------
    def error_message(self, response: httpx.Response) -> str:
        """A short, safe description. Never the raw body.

        Provider error bodies echo request fields and, on some providers, the
        offending header - which is the authorization header. Reading a message
        string out of the parsed JSON keeps the key out of the logs by
        construction rather than by a redaction pass that can be forgotten.
        """
        try:
            payload = response.json()
        except ValueError:
            return f"{response.status_code} {response.reason_phrase}"
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or response.reason_phrase)
        if isinstance(error, str):
            return error
        return str(payload.get("message") or response.reason_phrase)

    def map_error(self, response: httpx.Response) -> LLMError:
        status = response.status_code
        message = self.error_message(response)
        if status in (401, 403):
            return AuthFailed(
                message or "authentication rejected", provider=self.name, status=status
            )
        if status == 429:
            return RateLimited(
                message or "rate limited",
                provider=self.name,
                status=status,
                retry_after=_retry_after(response),
            )
        if status == 408 or status >= 500:
            return Transient(message or "provider error", provider=self.name, status=status)
        return InvalidRequest(message or "request rejected", provider=self.name, status=status)

    def map_body_error(self, error: dict[str, Any], status: int) -> LLMError:
        """A 200 that is really a failure. Status comes from the body if present."""
        code = error.get("code")
        effective = int(code) if isinstance(code, int) else status
        message = str(error.get("message") or "provider reported an error")
        if effective in (401, 403):
            return AuthFailed(message, provider=self.name, status=effective)
        if effective == 429:
            return RateLimited(message, provider=self.name, status=effective)
        if effective >= 500 or effective == 200:
            return Transient(message, provider=self.name, status=effective)
        return InvalidRequest(message, provider=self.name, status=effective)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<{type(self).__name__} model={self.model!r}>"


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


__all__ = ["DEFAULT_MAX_TOKENS", "DEFAULT_TIMEOUT", "OpenAICompatibleProvider"]
