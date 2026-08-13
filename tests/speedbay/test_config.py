"""Tests for the org-layer settings module (OPE-31).

Covers the env-override accessors; the plain constants are exercised by the
suites of the modules that consume them.

Run:  .venv/bin/python -m pytest tests/speedbay/test_config.py -x -q
"""

from __future__ import annotations

import pytest

from agent.speedbay import config, docker_sandbox


def test_sandbox_ttl_rejects_negative_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.TTL_ENV, "-1")
    with pytest.raises(
        ValueError,
        match=r"DOCKER_SANDBOX_TTL_SECONDS must be non-negative, got -1",
    ):
        config.sandbox_ttl_seconds()


def test_sweep_stops_before_docker_for_negative_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fail_docker(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("Docker must not be called for a negative TTL")

    monkeypatch.setenv(config.TTL_ENV, "-1")
    monkeypatch.setattr(docker_sandbox, "_docker", fail_docker)
    with pytest.raises(ValueError, match="must be non-negative"):
        docker_sandbox._sweep_expired()
    assert calls == []


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in (config.IMAGE_ENV, config.TTL_ENV, config.MEMORY_ENV):
        monkeypatch.delenv(env, raising=False)
    assert config.sandbox_image() == config.DEFAULT_IMAGE
    assert config.sandbox_ttl_seconds() == config.DEFAULT_TTL_SECONDS
    assert config.sandbox_memory() == config.DEFAULT_MEMORY


def test_env_overrides_resolve_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.IMAGE_ENV, "openswe-sandbox:test")
    monkeypatch.setenv(config.TTL_ENV, "0")
    monkeypatch.setenv(config.MEMORY_ENV, "1g")
    assert config.sandbox_image() == "openswe-sandbox:test"
    assert config.sandbox_ttl_seconds() == 0
    monkeypatch.setenv(config.TTL_ENV, "60")
    assert config.sandbox_ttl_seconds() == 60
    assert config.sandbox_memory() == "1g"


def test_verify_sweep_min_age_rejects_negative_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.VERIFY_SWEEP_MIN_AGE_ENV, "-1")
    with pytest.raises(ValueError, match="must be non-negative"):
        config.verify_sweep_min_age_seconds()
