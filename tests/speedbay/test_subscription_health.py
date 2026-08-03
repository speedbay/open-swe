"""Tests for nightly in-system subscription OAuth health monitoring (OPE-68)."""

from __future__ import annotations

from typing import Any

import pytest

from agent.speedbay import subscription_health
from agent.speedbay.subscription_auth import ENV_TOGGLE


def _model(class_name: str, *, error: Exception | None = None) -> object:
    async def ainvoke(_self: object, _prompt: str) -> object:
        if error:
            raise error
        return object()

    model_type = type(class_name, (), {"ainvoke": ainvoke})
    return model_type()


@pytest.fixture
def linear(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = variables or {}
        if "SubscriptionHealthIssue(" in query:
            title = variables["filter"]["title"]["eq"]
            result = {"issues": {"nodes": [{"id": f"{title}-id", "title": title}]}}
        elif "SubscriptionHealthCommentCreate" in query:
            result = {"commentCreate": {"success": True, "comment": {"id": "comment-id"}}}
        else:
            raise AssertionError(query)
        calls.append((query, variables))
        return result

    monkeypatch.setattr(subscription_health, "_graphql_request", fake_graphql)
    return calls


def _comments(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [variables for query, variables in calls if "SubscriptionHealthCommentCreate" in query]


async def test_disabled_skips_models_and_posts_heartbeat(
    monkeypatch: pytest.MonkeyPatch, linear: list[tuple[str, dict[str, Any]]]
) -> None:
    monkeypatch.delenv(ENV_TOGGLE, raising=False)
    monkeypatch.setattr(
        subscription_health,
        "subscription_model",
        lambda *_args: pytest.fail("disabled health check must not construct models"),
    )

    result = await subscription_health.check_subscription_auth()

    assert result == {"status": "disabled"}
    comments = _comments(linear)
    assert len(comments) == 1
    assert "disabled" in comments[0]["body"]


async def test_wrong_class_alerts_and_posts_heartbeat(
    monkeypatch: pytest.MonkeyPatch, linear: list[tuple[str, dict[str, Any]]]
) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "1")
    monkeypatch.setattr(subscription_health, "subscription_model", lambda *_args: object())

    result = await subscription_health.check_subscription_auth()

    assert result["status"] == "failed"
    assert all(verdict["class"] == "object" for verdict in result["providers"].values())
    comments = _comments(linear)
    assert len(comments) == 2
    assert any(call["issueId"] == f"{subscription_health.ALERT_TITLE}-id" for call in comments)
    assert any(call["issueId"] == f"{subscription_health.HEARTBEAT_TITLE}-id" for call in comments)


async def test_healthy_models_only_post_heartbeat(
    monkeypatch: pytest.MonkeyPatch, linear: list[tuple[str, dict[str, Any]]]
) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "true")

    def construct(model_id: str, _kwargs: dict[str, object]) -> object:
        expected = subscription_health._PROVIDER_MODELS[model_id.split(":", 1)[0]][1]
        return _model(expected)

    monkeypatch.setattr(subscription_health, "subscription_model", construct)

    result = await subscription_health.check_subscription_auth()

    assert result["status"] == "healthy"
    comments = _comments(linear)
    assert len(comments) == 1
    assert comments[0]["issueId"] == f"{subscription_health.HEARTBEAT_TITLE}-id"
    assert "succeeded" in comments[0]["body"]


async def test_existing_alert_is_commented_not_created(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, variables))
        if "SubscriptionHealthIssue(" in query:
            return {"issues": {"nodes": [{"id": "existing-alert"}]}}
        if "SubscriptionHealthCommentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(subscription_health, "_graphql_request", fake_graphql)
    await subscription_health._write_alert(
        {"openai": {"status": "failed", "class": "object", "live_call": "not run"}},
        "2026-08-02T13:00:00+00:00",
    )

    assert any(variables.get("issueId") == "existing-alert" for _, variables in calls)
    assert not any("SubscriptionHealthIssueCreate" in query for query, _ in calls)


async def test_missing_alert_is_created_and_commented(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append((query, variables))
        if "SubscriptionHealthIssue(" in query:
            return {"issues": {"nodes": []}}
        if "SubscriptionHealthTeam" in query:
            return {
                "teams": {"nodes": [{"id": "ope-team", "states": {"nodes": [{"id": "backlog"}]}}]}
            }
        if "SubscriptionHealthIssueCreate" in query:
            return {"issueCreate": {"success": True, "issue": {"id": "new-alert"}}}
        if "SubscriptionHealthCommentCreate" in query:
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(subscription_health, "_graphql_request", fake_graphql)
    await subscription_health._write_alert(
        {"openai": {"status": "failed", "class": "object", "live_call": "not run"}},
        "2026-08-02T13:00:00+00:00",
    )

    create = next(variables["input"] for query, variables in calls if "IssueCreate" in query)
    assert create["title"] == subscription_health.ALERT_TITLE
    assert create["stateId"] == "backlog"
    assert any(variables.get("issueId") == "new-alert" for _, variables in calls)


async def test_missing_heartbeat_is_created_with_cbass_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_inputs: list[dict[str, Any]] = []

    async def fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "SubscriptionHealthIssue(" in query:
            return {"issues": {"nodes": []}}
        if "SubscriptionHealthSubscriber" in query:
            assert variables == {"email": "cbass@speedbay.com"}
            return {"users": {"nodes": [{"id": "cbass-user"}]}}
        if "SubscriptionHealthTeam" in query:
            return {
                "teams": {"nodes": [{"id": "ope-team", "states": {"nodes": [{"id": "backlog"}]}}]}
            }
        if "SubscriptionHealthIssueCreate" in query:
            create_inputs.append(variables["input"])
            return {"issueCreate": {"success": True, "issue": {"id": "new-heartbeat"}}}
        if "SubscriptionHealthCommentCreate" in query:
            assert variables["issueId"] == "new-heartbeat"
            return {"commentCreate": {"success": True}}
        raise AssertionError(query)

    monkeypatch.setattr(subscription_health, "_graphql_request", fake_graphql)

    await subscription_health._write_heartbeat("disabled", {}, "2026-08-02T13:00:00+00:00")

    assert create_inputs[0]["title"] == subscription_health.HEARTBEAT_TITLE
    assert create_inputs[0]["stateId"] == "backlog"
    assert create_inputs[0]["subscriberIds"] == ["cbass-user"]


async def test_heartbeat_failure_does_not_change_verdict_or_suppress_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_TOGGLE, "on")
    monkeypatch.setattr(subscription_health, "subscription_model", lambda *_args: object())
    alert_calls: list[bool] = []

    async def fake_alert(*_args: object) -> None:
        alert_calls.append(True)

    async def fail_heartbeat(*_args: object) -> None:
        raise RuntimeError("Linear unavailable")

    monkeypatch.setattr(subscription_health, "_write_alert", fake_alert)
    monkeypatch.setattr(subscription_health, "_write_heartbeat", fail_heartbeat)

    result = await subscription_health.check_subscription_auth()

    assert result["status"] == "failed"
    assert alert_calls == [True]


async def test_scheduler_routes_subscription_health(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import scheduler

    async def fake_check() -> dict[str, str]:
        return {"status": "healthy"}

    monkeypatch.setattr(subscription_health, "check_subscription_auth", fake_check)
    graph = scheduler.get_scheduler()
    result = await graph.ainvoke({}, config={"configurable": {"task": "subscription_health"}})

    assert result["result"] == {"status": "healthy"}
