"""Tests for the org-layer settings module (OPE-31).

Covers the env-override accessors; the plain constants are exercised by the
suites of the modules that consume them.

Run:  .venv/bin/python -m pytest tests/speedbay/test_config.py -x -q
"""

from __future__ import annotations

import pytest

from agent.speedbay import config


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (config.IMAGE_ENV, config.TTL_ENV, config.MEMORY_ENV):
        monkeypatch.delenv(env, raising=False)
    assert config.sandbox_image() == config.DEFAULT_IMAGE
    assert config.sandbox_ttl_seconds() == config.DEFAULT_TTL_SECONDS
    assert config.sandbox_memory() == config.DEFAULT_MEMORY


def test_env_overrides_resolve_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.IMAGE_ENV, "openswe-sandbox:test")
    monkeypatch.setenv(config.TTL_ENV, "60")
    monkeypatch.setenv(config.MEMORY_ENV, "1g")
    assert config.sandbox_image() == "openswe-sandbox:test"
    assert config.sandbox_ttl_seconds() == 60
    assert config.sandbox_memory() == "1g"
