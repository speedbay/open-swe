"""Nightly in-system subscription OAuth health check (OPE-68)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..utils.linear import _graphql_request
from .subscription_auth import _enabled, subscription_model

logger = logging.getLogger(__name__)
ALERT_TITLE = "Subscription auth degraded"
HEARTBEAT_TITLE = "Subscription auth nightly heartbeat"
_PROVIDER_MODELS = {
    "openai": ("openai:gpt-5.6-sol", "_ChatOpenAICodex"),
    "anthropic": ("anthropic:claude-opus-5", "ChatClaudeCode"),
}
_FIND = """query SubscriptionHealthIssue($filter: IssueFilter!) {
  issues(filter: $filter, first: 1) { nodes { id } }
}"""
_TEAM = """query SubscriptionHealthTeam($key: String!, $state: String!) {
  teams(filter: {key: {eq: $key}}, first: 1) {
    nodes { id states(filter: {name: {eq: $state}}, first: 1) { nodes { id } } }
  }
}"""
_USER = """query SubscriptionHealthSubscriber($email: String!) {
  users(filter: {email: {eq: $email}}, first: 1) { nodes { id } }
}"""
_CREATE = """mutation SubscriptionHealthIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id } }
}"""
_COMMENT = """mutation SubscriptionHealthCommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success }
}"""


def _field(result: dict[str, Any], name: str) -> dict[str, Any]:
    value = result.get(name)
    if "error" in result or not isinstance(value, dict):
        raise RuntimeError(str(result.get("error") or f"Linear response missing {name}"))
    return value


async def _find(title: str) -> dict[str, Any] | None:
    result = await _graphql_request(
        _FIND,
        {
            "filter": {
                "team": {"key": {"eq": "OPE"}},
                "title": {"eq": title},
                "state": {"type": {"nin": ["completed", "canceled"]}},
            }
        },
    )
    nodes = _field(result, "issues").get("nodes") or []
    return nodes[0] if nodes else None


async def _create(title: str, description: str, subscribers: list[str] | None = None) -> dict:
    result = await _graphql_request(_TEAM, {"key": "OPE", "state": "Backlog"})
    teams = _field(result, "teams").get("nodes") or []
    states = (teams[0].get("states") or {}).get("nodes") if teams else []
    if not teams or not states:
        raise RuntimeError("Linear OPE team or Backlog state was not found")
    issue_input = {
        "teamId": teams[0]["id"],
        "stateId": states[0]["id"],
        "title": title,
        "description": description,
    }
    if subscribers is not None:
        issue_input["subscriberIds"] = subscribers
    created = _field(await _graphql_request(_CREATE, {"input": issue_input}), "issueCreate")
    if not created.get("success") or not created.get("issue"):
        raise RuntimeError(f"Linear did not create {title}")
    return created["issue"]


async def _comment(issue_id: str, body: str) -> None:
    result = _field(
        await _graphql_request(_COMMENT, {"issueId": issue_id, "body": body}),
        "commentCreate",
    )
    if not result.get("success"):
        raise RuntimeError(f"Linear did not comment on {issue_id}")


def _lines(providers: dict[str, dict[str, str]]) -> list[str]:
    lines = []
    for name, verdict in providers.items():
        line = f"- **{name}**: class `{verdict['class']}`; live call `{verdict['live_call']}`"
        if verdict.get("error"):
            line += f"; `{verdict['error']}`"
        lines.append(line)
    return lines


async def _write_alert(providers: dict[str, dict[str, str]], timestamp: str) -> None:
    failures = {name: value for name, value in providers.items() if value["status"] == "failed"}
    recovery = "Recovery: see `OPERATIONS.md` § Subscription OAuth."
    body = "\n".join(
        [f"Subscription OAuth check failed at {timestamp}.", "", *_lines(failures), "", recovery]
    )
    issue = await _find(ALERT_TITLE)
    if issue is None:
        issue = await _create(ALERT_TITLE, f"Nightly subscription OAuth alert.\n\n{recovery}")
    await _comment(str(issue["id"]), body)


async def _write_heartbeat(
    status: str, providers: dict[str, dict[str, str]], timestamp: str
) -> None:
    issue = await _find(HEARTBEAT_TITLE)
    if issue is None:
        users = (
            _field(await _graphql_request(_USER, {"email": "cbass@speedbay.com"}), "users").get(
                "nodes"
            )
            or []
        )
        if not users:
            raise RuntimeError("Linear user cbass@speedbay.com was not found")
        issue = await _create(
            HEARTBEAT_TITLE,
            "Nightly subscription OAuth status heartbeat.",
            [str(users[0]["id"])],
        )
    lines = [f"- UTC: `{timestamp}`", f"- Overall status: **{status}**"]
    lines += ["- Subscription auth: `disabled`"] if status == "disabled" else _lines(providers)
    await _comment(str(issue["id"]), "\n".join(lines))


async def check_subscription_auth() -> dict[str, Any]:
    """Probe both OAuth models and publish alert and heartbeat status."""
    timestamp = datetime.now(UTC).isoformat()
    if not _enabled():
        try:
            await _write_heartbeat("disabled", {}, timestamp)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write subscription-auth heartbeat")
        return {"status": "disabled"}

    providers: dict[str, dict[str, str]] = {}
    for provider, (model_id, expected) in _PROVIDER_MODELS.items():
        verdict = {"status": "failed", "class": "not constructed", "live_call": "not run"}
        try:
            model = subscription_model(model_id, {"max_tokens": 1})
            verdict["class"] = type(model).__name__
            if model is None or verdict["class"] != expected:
                verdict["error"] = f"expected {expected}"
            else:
                await model.ainvoke("Reply OK")
                verdict.update(status="healthy", live_call="succeeded")
        except Exception as exc:  # noqa: BLE001
            verdict.update(error=f"{type(exc).__name__}: {exc}", live_call="failed")
        providers[provider] = verdict

    status = "failed" if any(v["status"] == "failed" for v in providers.values()) else "healthy"
    if status == "failed":
        try:
            await _write_alert(providers, timestamp)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write subscription-auth degradation alert")
    try:
        await _write_heartbeat(status, providers, timestamp)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write subscription-auth heartbeat")
    return {"status": status, "providers": providers}
