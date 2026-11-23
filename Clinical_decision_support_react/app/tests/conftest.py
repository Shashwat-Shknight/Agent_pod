"""Unit tests run against the local JSON backend so they never touch the shared database.

Snowflake integration tests opt in explicitly (see test_snowflake_integration.py).
"""

import os

import pytest

from app.db import settings

os.environ["STORAGE_BACKEND"] = "json"
# The original route tests call endpoints without a session; web API tests re-enable auth explicitly.
os.environ["NUUCARE_AUTH_DISABLED"] = "1"
settings.reset_cache()


@pytest.fixture(autouse=True)
def _json_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "json")
    settings.reset_cache()
    # Unit tests exercise the Coforge client; a developer's personal GROK_API_KEY in .env must not reroute them.
    from app import llm_client

    monkeypatch.setattr(llm_client, "_grok_settings", lambda: None)
    yield
    settings.reset_cache()
