"""One error vocabulary for every provider.

Groq and OpenRouter disagree about status codes, body shapes and which of their
failures are worth retrying. The router must not care. Each client maps its own
provider's failures onto these four, and the router's retry and failover policy
is written against the taxonomy rather than against any provider's HTTP quirks.

The split that matters is *retry here* versus *move on*:

- `RateLimited` and `Transient` are retried with backoff, then the chain moves
  to the next provider.
- `AuthFailed` and `InvalidRequest` are not retried at all. A bad key does not
  become a good key after three seconds, and a prompt the provider rejected as
  malformed will be rejected identically on the next attempt - retrying either
  just spends the request budget to reach the same answer more slowly.

`ProviderUnavailable` is the local case: no key configured, so the client was
never constructed. It is not an error the user has to act on when another
provider in the chain can answer, which is why it carries no HTTP detail.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base for everything this layer raises."""

    retryable = False

    def __init__(self, message: str, *, provider: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status

    def __str__(self) -> str:
        head = super().__str__()
        where = f"[{self.provider}]" if self.provider else ""
        code = f" (HTTP {self.status})" if self.status else ""
        return f"{where}{' ' if where else ''}{head}{code}".strip()


class RateLimited(LLMError):
    """429, or a provider-specific quota message. Retry after a wait."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status=status)
        self.retry_after = retry_after


class Transient(LLMError):
    """5xx, timeout, connection reset. Retry."""

    retryable = True


class InvalidRequest(LLMError):
    """4xx that is our fault: bad model id, oversized prompt, bad parameters."""


class AuthFailed(LLMError):
    """401/403. The key is missing, wrong or revoked."""


class ProviderUnavailable(LLMError):
    """Not configured in this process - no key, so no client."""


class AllProvidersFailed(LLMError):
    """Every provider in the chain was tried and none answered.

    Carries the per-provider failure so the log line says which provider failed
    for which reason, rather than one flattened message that hides whether the
    chain died of a bad key or of rate limits.
    """

    def __init__(self, failures: dict[str, Exception]) -> None:
        detail = "; ".join(f"{name}: {err}" for name, err in failures.items()) or "chain empty"
        super().__init__(f"no provider answered - {detail}")
        self.failures = dict(failures)


__all__ = [
    "AllProvidersFailed",
    "AuthFailed",
    "InvalidRequest",
    "LLMError",
    "ProviderUnavailable",
    "RateLimited",
    "Transient",
]
