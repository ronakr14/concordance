"""Test isolation from the developer's own environment.

`Settings` reads `.env` and the process environment, which is correct for the
application and wrong for a test suite: whether a test passes must not depend on
which keys, models or paths the person running it happens to have configured.

This was not hypothetical. Creating a `.env` with `GROQ_MODEL` set turned two
passing router tests red, because `Settings()` inside the test picked up the
developer's model rather than the provider default the test was asserting about.
The tests were right and the isolation was missing.

So for the whole session: `.env` is ignored, and every `CONCORDANCE`-relevant
variable is cleared from the environment. A test that wants a setting passes it
explicitly, which is also how the assertion stays readable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from concordance.config import Settings, get_settings

#: Every setting that could plausibly be set on a developer machine and change
#: a test's answer. Cleared for the session rather than per test, because the
#: contamination is ambient rather than something one test does to another.
ISOLATED_VARS = tuple(Settings.model_fields)


@pytest.fixture(autouse=True, scope="session")
def _isolate_settings() -> Iterator[None]:
    original_env_file = Settings.model_config.get("env_file")
    saved = {name: os.environ.pop(name, None) for name in ISOLATED_VARS}
    saved.update({name.lower(): os.environ.pop(name.lower(), None) for name in ISOLATED_VARS})
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original_env_file
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
        get_settings.cache_clear()


def test_the_isolation_actually_works() -> None:
    """A guard on the guard: if `.env` leaked in, every other test is suspect."""
    settings = Settings()
    assert settings.GROQ_API_KEY is None
    assert settings.OPENROUTER_API_KEY is None
    assert settings.LLM_ENABLED is False
    assert settings.LLM_MODEL is None
    assert settings.GROQ_MODEL is None
