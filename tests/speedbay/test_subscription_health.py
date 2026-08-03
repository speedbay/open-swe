"""Tests for nightly subscription OAuth health monitoring (OPE-68)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.speedbay import subscription_health as health
from agent.speedbay.subscription_auth import ENV_TOGGLE


def _model(name: str) -> object:
    async def ainvoke(_self: object, _prompt: str) -> object:
        return object()

    return type(name, (), {"ainvoke": ainvoke})()


@pytest.fixture
def linear(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls = []

    async def graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = variables or {}
        calls.append((query, variables))
        if "SubscriptionHealthIssue(" in query:
            title = variables["filter"]["title"]["eq"]
            return {"issues": {"nodes": [{"id": f"{title}-id"}]}}
        if "SubscriptionHealthCommentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(health, "_graphql_request", graphql)
    return calls


def _comments(calls: list[tuple[str, dict]]) -> list[dict]:
    return [variables for query, variables in calls if "CommentCreate" in query]


async def test_disabled_skips_models_and_posts_heartbeat(
    monkeypatch: pytest.MonkeyPatch, linear: list[tuple[str, dict]]
) -> None:
    monkeypatch.delenv(ENV_TOGGLE, raising=False)
    monkeypatch.setattr(
        health, "subscription_model", lambda *_args: pytest.fail("must not construct")
    )
    assert await health.check_subscription_auth() == {"status": "disabled"}
    assert len(_comments(linear)) == 1
    assert "disabled" in _comments(linear)[0]["body"]


async def test_wrong_class_alerts_and_posts_heartbeat(
    monkeypatch: pytest.MonkeyPatch, linear: list[tuple[str, dict]]
) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "1")
    monkeypatch.setattr(health, "subscription_model", lambda *_args: object())
    result = await health.check_subscription_auth()
    assert result["status"] == "failed"
    assert all(value["class"] == "object" for value in result["providers"].values())
    assert {call["issueId"] for call in _comments(linear)} == {
        f"{health.ALERT_TITLE}-id",
        f"{health.HEARTBEAT_TITLE}-id",
    }


async def test_healthy_models_only_post_heartbeat(
    monkeypatch: pytest.MonkeyPatch, linear: list[tuple[str, dict]]
) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "true")
    monkeypatch.setattr(
        health,
        "subscription_model",
        lambda model_id, _kwargs: _model(health._PROVIDER_MODELS[model_id.split(":")[0]][1]),
    )
    result = await health.check_subscription_auth()
    assert result["status"] == "healthy"
    assert len(_comments(linear)) == 1
    assert _comments(linear)[0]["issueId"] == f"{health.HEARTBEAT_TITLE}-id"


async def test_alert_dedupes_to_existing_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def graphql(query: str, variables: dict) -> dict:
        calls.append((query, variables))
        if "SubscriptionHealthIssue(" in query:
            return {"issues": {"nodes": [{"id": "existing"}]}}
        if "CommentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(health, "_graphql_request", graphql)
    await health._write_alert(
        {"openai": {"status": "failed", "class": "object", "live_call": "not run"}}, "now"
    )
    assert any(variables.get("issueId") == "existing" for _, variables in calls)
    assert not any("IssueCreate" in query for query, _ in calls)


async def test_absent_alert_is_created_in_backlog_and_commented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = []

    async def graphql(query: str, variables: dict) -> dict:
        if "SubscriptionHealthIssue(" in query:
            return {"issues": {"nodes": []}}
        if "SubscriptionHealthTeam" in query:
            return {"teams": {"nodes": [{"id": "ope", "states": {"nodes": [{"id": "backlog"}]}}]}}
        if "IssueCreate" in query:
            inputs.append(variables["input"])
            return {"issueCreate": {"success": True, "issue": {"id": "new"}}}
        if "CommentCreate" in query:
            assert variables["issueId"] == "new"
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(health, "_graphql_request", graphql)
    await health._write_alert(
        {"openai": {"status": "failed", "class": "object", "live_call": "not run"}}, "now"
    )
    assert inputs[0]["title"] == health.ALERT_TITLE
    assert inputs[0]["stateId"] == "backlog"


async def test_absent_heartbeat_is_created_with_cbass_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = []

    async def graphql(query: str, variables: dict) -> dict:
        if "SubscriptionHealthIssue(" in query:
            return {"issues": {"nodes": []}}
        if "SubscriptionHealthSubscriber" in query:
            return {"users": {"nodes": [{"id": "cbass"}]}}
        if "SubscriptionHealthTeam" in query:
            return {"teams": {"nodes": [{"id": "ope", "states": {"nodes": [{"id": "backlog"}]}}]}}
        if "IssueCreate" in query:
            inputs.append(variables["input"])
            return {"issueCreate": {"success": True, "issue": {"id": "heartbeat"}}}
        if "CommentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(health, "_graphql_request", graphql)
    await health._write_heartbeat("disabled", {}, "now")
    assert inputs[0]["subscriberIds"] == ["cbass"]


async def test_heartbeat_failure_preserves_verdict_and_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "on")
    monkeypatch.setattr(health, "subscription_model", lambda *_args: object())
    alerts = []

    async def alert(*_args: object) -> None:
        alerts.append(True)

    async def heartbeat(*_args: object) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr(health, "_write_alert", alert)
    monkeypatch.setattr(health, "_write_heartbeat", heartbeat)
    assert (await health.check_subscription_auth())["status"] == "failed"
    assert alerts == [True]


async def test_scheduler_routes_subscription_health(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import scheduler

    async def check() -> dict[str, str]:
        return {"status": "healthy"}

    monkeypatch.setattr(health, "check_subscription_auth", check)
    result = await scheduler.get_scheduler().ainvoke(
        {}, config={"configurable": {"task": "subscription_health"}}
    )
    assert result["result"] == {"status": "healthy"}
