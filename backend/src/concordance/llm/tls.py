"""TLS verification that survives a corporate proxy, without weakening it.

Many corporate networks - this project's development machine included - run a
TLS-inspecting proxy. It terminates the connection and re-signs it with a
private root CA that is installed in the operating system's certificate store.
Python does not use that store by default: `httpx` verifies against `certifi`,
a fixed bundle of public roots that by definition cannot contain a private one.
The result is `CERTIFICATE_VERIFY_FAILED` on every call, from a machine whose
browser reaches the same URL happily.

There are three ways out and only two are acceptable.

1. **`verify=False`.** Never. It disables verification globally for the client,
   so the process would accept *any* certificate from anyone - including on a
   network with no proxy at all, where the failure this "fixes" does not exist.
   That is a real downgrade traded for a local convenience, and it is not
   offered here even as an option.
2. **The OS trust store**, via `truststore`. The platform verifier already
   knows every root the machine trusts, public and private, and it is what the
   browser uses. This is *stronger* than the certifi default, not weaker: the
   set of trusted roots is the administrator's decision rather than a bundled
   snapshot. This is the default when `truststore` is installed.
3. **An explicit CA bundle** at `LLM_CA_BUNDLE`, for a machine whose proxy root
   is a file rather than an installed certificate.

If `truststore` is absent and no bundle is configured, the context falls back to
`httpx`'s own certifi default - correct on a normal network, and failing loudly
rather than silently insecurely on a proxied one.
"""

from __future__ import annotations

import ssl
from pathlib import Path

from concordance.logging_setup import get_logger

log = get_logger("llm.tls")


def ssl_context(ca_bundle: Path | str | None = None) -> ssl.SSLContext | None:
    """A verifying SSL context, or `None` to keep httpx's default.

    Verification is always on. The only question this answers is *which* roots
    are trusted.
    """
    if ca_bundle:
        path = Path(ca_bundle)
        if not path.exists():
            log.warning("llm.tls.ca_bundle_missing", path=str(path))
        else:
            log.info("llm.tls.ca_bundle", path=str(path))
            return ssl.create_default_context(cafile=str(path))

    try:
        import truststore
    except ImportError:
        log.debug("llm.tls.default", reason="truststore not installed")
        return None
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


__all__ = ["ssl_context"]
