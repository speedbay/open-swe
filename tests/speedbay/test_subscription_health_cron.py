"""Tests for nightly subscription-health cron provisioning (OPE-68)."""

from typing import Any

import pytest

from agent.speedbay import subscription_health_cron as cron


class _Crons:
    def __init__(self, existing: bool = False) -> None:
        self.existing = [{"cron_id": "existing"}] if existing else []
        self.created = []

    async def search(self, **kwargs: Any) -> list[dict]:
        assert kwargs == {"metadata": {"kind": "subscription_health"}, "limit": 1}
        return self.existing

    async def create(self, *args: Any, **kwargs: Any) -> dict:
        self.created.append((args, kwargs))
        return {"cron_id": "new"}


class _Client:
    def __init__(self, crons: _Crons) -> None:
        self.crons = crons
        self.threads = self

    async def create(self, **kwargs: Any) -> dict:
        return {"metadata": kwargs["metadata"]}

    async def delete(self, _thread_id: str) -> None:
        return None


@pytest.mark.parametrize("existing", [False, True])
async def test_cron_ensure_is_metadata_idempotent(
    monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    crons = _Crons(existing)
    monkeypatch.setattr(cron, "langgraph_client", lambda: _Client(crons))
    result = await cron.ensure_subscription_health_cron()
    assert result == ("existing:existing" if existing else "created:new")
    if not existing:
        args, kwargs = crons.created[0]
        assert args == ("scheduler",)
        assert kwargs["schedule"] == "0 13 * * *"
        assert kwargs["config"] == {"configurable": {"task": "subscription_health"}}


def test_docker_boot_schedules_both_org_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.speedbay import docker_sandbox, verify_sweep_cron
    from agent.utils import sandbox

    calls = []
    monkeypatch.setenv("SANDBOX_TYPE", "docker")
    monkeypatch.setattr(docker_sandbox, "validate_startup_config", lambda: calls.append("validate"))
    monkeypatch.setattr(
        verify_sweep_cron, "schedule_verify_sweep_cron_ensure", lambda: calls.append("verify")
    )
    monkeypatch.setattr(
        cron, "schedule_subscription_health_cron_ensure", lambda: calls.append("health")
    )
    sandbox.validate_sandbox_startup_config()
    assert calls == ["validate", "verify", "health"]
