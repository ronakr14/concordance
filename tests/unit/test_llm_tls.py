"""TLS verification stays on. The only question is which roots are trusted."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from concordance.config import Settings
from concordance.llm.router import build_providers
from concordance.llm.tls import ssl_context

pytestmark = pytest.mark.unit


def test_an_explicit_ca_bundle_is_used(tmp_path) -> None:
    import certifi

    # A real PEM is required: `create_default_context(cafile=...)` parses it.
    bundle = tmp_path / "corp-root.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes())
    context = ssl_context(bundle)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_missing_bundle_falls_back_rather_than_crashing(tmp_path) -> None:
    context = ssl_context(tmp_path / "nonexistent.pem")
    assert context is None or isinstance(context, ssl.SSLContext)


def test_the_default_context_still_verifies() -> None:
    context = ssl_context(None)
    if context is not None:
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True


def test_verification_is_never_disabled() -> None:
    """There is no code path that produces an unverified context.

    `verify=False` would accept any certificate from anyone, including on a
    network with no proxy, so it is not offered even as an option.
    """
    for argument in (None, "", "does-not-exist.pem"):
        context = ssl_context(argument)
        if context is not None:
            assert context.verify_mode is not ssl.CERT_NONE


def test_the_ca_bundle_setting_reaches_the_provider(tmp_path) -> None:
    bundle = tmp_path / "corp-root.pem"
    bundle.write_text("", encoding="utf-8")

    settings = Settings(LLM_PROVIDER_CHAIN="groq", GROQ_API_KEY="k", LLM_CA_BUNDLE=bundle)
    provider = build_providers(settings)[0]
    assert provider.ca_bundle == bundle
