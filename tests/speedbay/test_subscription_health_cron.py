"""Tests for nightly subscription-health cron provisioning (OPE-68)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.speedbay import subscription_health_cron


class _Crons:
    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.existing = existing or []
        self.created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"metadata": {"kind": "subscription_health"}, "limit": 1}
        return self.existing

    async def create(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        self.created.append((args, kwargs))
        return {"cron_id": "health-cron"}


class _Threads:
    async def create(self, **kwargs: Any) -> dict[str, Any]:
        return {"metadata": kwargs["metadata"]}

    async def delete(self, _thread_id: str) -> None:
        return None


class _Client:
    def __init__(self, crons: _Crons) -> None:
        self.crons = crons
        self.threads = _Threads()


async def test_absent_cron_is_created_with_nightly_task(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _Crons()
    monkeypatch.setattr(subscription_health_cron, "langgraph_client", lambda: _Client(crons))

    result = await subscription_health_cron.ensure_subscription_health_cron()

    assert result == "created:health-cron"
    assert crons.created == [
        (
            ("scheduler",),
            {
                "schedule": "0 13 * * *",
                "input": {},
                "config": {"configurable": {"task": "subscription_health"}},
                "metadata": {"kind": "subscription_health"},
            },
        )
    ]


async def test_existing_metadata_keyed_cron_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _Crons([{"cron_id": "existing-health"}])
    monkeypatch.setattr(subscription_health_cron, "langgraph_client", lambda: _Client(crons))

    result = await subscription_health_cron.ensure_subscription_health_cron()

    assert result == "existing:existing-health"
    assert crons.created == []


def test_docker_boot_schedules_both_org_cron_ensures(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.speedbay import docker_sandbox, verify_sweep_cron
    from agent.utils import sandbox

    calls: list[str] = []
    monkeypatch.setenv("SANDBOX_TYPE", "docker")
    monkeypatch.setattr(docker_sandbox, "validate_startup_config", lambda: calls.append("validate"))
    monkeypatch.setattr(
        verify_sweep_cron, "schedule_verify_sweep_cron_ensure", lambda: calls.append("verify")
    )
    monkeypatch.setattr(
        subscription_health_cron,
        "schedule_subscription_health_cron_ensure",
        lambda: calls.append("health"),
    )

    sandbox.validate_sandbox_startup_config()

    assert calls == ["validate", "verify", "health"]
