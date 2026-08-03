"""Tests for nightly subscription OAuth health monitoring (OPE-68)."""

from typing import Any

import pytest

from agent.speedbay import subscription_health as health
from agent.speedbay.subscription_auth import ENV_TOGGLE


def _model(name: str) -> object:
    async def ainvoke(_self: object, _prompt: str) -> object:
        return object()

    return type(name, (), {"ainvoke": ainvoke})()


def _graphql(existing: bool = True) -> tuple[list[tuple[str, dict]], Any]:
    calls = []

    async def graphql(query: str, variables: dict | None = None) -> dict:
        variables = variables or {}
        calls.append((query, variables))
        if "SubscriptionHealthIssue(" in query:
            title = variables["filter"]["title"]["eq"]
            return {"issues": {"nodes": [{"id": title}] if existing else []}}
        if "SubscriptionHealthTeam" in query:
            return {"teams": {"nodes": [{"id": "ope", "states": {"nodes": [{"id": "backlog"}]}}]}}
        if "SubscriptionHealthSubscriber" in query:
            return {"users": {"nodes": [{"id": "cbass"}]}}
        if "IssueCreate" in query:
            return {"issueCreate": {"success": True, "issue": {"id": "new"}}}
        if "CommentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    return calls, graphql


def _comments(calls: list[tuple[str, dict]]) -> list[dict]:
    return [variables for query, variables in calls if "CommentCreate" in query]


@pytest.mark.parametrize("enabled,status", [(False, "disabled"), (True, "healthy")])
async def test_every_outcome_posts_heartbeat(monkeypatch, enabled, status) -> None:
    calls, graphql = _graphql()
    monkeypatch.setattr(health, "_graphql_request", graphql)
    if enabled:
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(
            health,
            "subscription_model",
            lambda model_id, _: _model(health._PROVIDER_MODELS[model_id.split(":")[0]][1]),
        )
    else:
        monkeypatch.delenv(ENV_TOGGLE, raising=False)
        monkeypatch.setattr(health, "subscription_model", lambda *_: pytest.fail("constructed"))
    assert (await health.check_subscription_auth())["status"] == status
    assert _comments(calls)[0]["issueId"] == health.HEARTBEAT_TITLE


async def test_wrong_class_alerts_and_posts_heartbeat(monkeypatch) -> None:
    calls, graphql = _graphql()
    monkeypatch.setattr(health, "_graphql_request", graphql)
    monkeypatch.setenv(ENV_TOGGLE, "1")
    monkeypatch.setattr(health, "subscription_model", lambda *_: object())
    result = await health.check_subscription_auth()
    assert result["status"] == "failed"
    assert all(value["class"] == "object" for value in result["providers"].values())
    assert {call["issueId"] for call in _comments(calls)} == {
        health.ALERT_TITLE,
        health.HEARTBEAT_TITLE,
    }


async def test_alert_dedupes_to_comment(monkeypatch) -> None:
    calls, graphql = _graphql()
    monkeypatch.setattr(health, "_graphql_request", graphql)
    await health._write_alert(
        {"openai": {"status": "failed", "class": "object", "live_call": "not run"}}, "now"
    )
    assert _comments(calls)[0]["issueId"] == health.ALERT_TITLE
    assert not any("IssueCreate" in query for query, _ in calls)


@pytest.mark.parametrize("heartbeat", [False, True])
async def test_absent_issue_create_paths(monkeypatch, heartbeat) -> None:
    calls, graphql = _graphql(existing=False)
    monkeypatch.setattr(health, "_graphql_request", graphql)
    providers = {"openai": {"status": "failed", "class": "object", "live_call": "not run"}}
    if heartbeat:
        await health._write_heartbeat("disabled", {}, "now")
    else:
        await health._write_alert(providers, "now")
    issue_input = next(variables["input"] for query, variables in calls if "IssueCreate" in query)
    assert issue_input["stateId"] == "backlog"
    assert not heartbeat or issue_input["subscriberIds"] == ["cbass"]
    assert len(_comments(calls)) == 1


async def test_heartbeat_failure_preserves_verdict_and_alert(monkeypatch) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "on")
    monkeypatch.setattr(health, "subscription_model", lambda *_: object())
    alerts = []

    async def alert(*_) -> None:
        alerts.append(True)

    async def heartbeat(*_) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(health, "_write_alert", alert)
    monkeypatch.setattr(health, "_write_heartbeat", heartbeat)
    assert (await health.check_subscription_auth())["status"] == "failed"
    assert alerts == [True]


async def test_scheduler_routes_subscription_health(monkeypatch) -> None:
    from agent import scheduler

    async def check() -> dict[str, str]:
        return {"status": "healthy"}

    monkeypatch.setattr(health, "check_subscription_auth", check)
    result = await scheduler.get_scheduler().ainvoke(
        {}, config={"configurable": {"task": "subscription_health"}}
    )
    assert result["result"] == {"status": "healthy"}
