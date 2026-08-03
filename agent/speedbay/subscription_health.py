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
TEAM_KEY = "OPE"
HEARTBEAT_SUBSCRIBER_EMAIL = "cbass@speedbay.com"

_PROVIDER_MODELS = {
    "openai": ("openai:gpt-5.6-sol", "_ChatOpenAICodex"),
    "anthropic": ("anthropic:claude-opus-5", "ChatClaudeCode"),
}

_FIND_OPEN_ISSUE = """
query SubscriptionHealthIssue($filter: IssueFilter!) {
  issues(filter: $filter, first: 1) {
    nodes { id identifier title }
  }
}
"""

_TEAM_AND_STATE = """
query SubscriptionHealthTeam($key: String!, $state: String!) {
  teams(filter: {key: {eq: $key}}, first: 1) {
    nodes {
      id
      states(filter: {name: {eq: $state}}, first: 1) { nodes { id } }
    }
  }
}
"""

_USER_BY_EMAIL = """
query SubscriptionHealthSubscriber($email: String!) {
  users(filter: {email: {eq: $email}}, first: 1) { nodes { id } }
}
"""

_CREATE_ISSUE = """
mutation SubscriptionHealthIssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier }
  }
}
"""

_CREATE_COMMENT = """
mutation SubscriptionHealthCommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success comment { id } }
}
"""


def _require(result: dict[str, Any], field: str) -> dict[str, Any]:
    if "error" in result:
        raise RuntimeError(str(result["error"]))
    value = result.get(field)
    if not isinstance(value, dict):
        raise RuntimeError(f"Linear response missing {field}")
    return value


async def _find_open_issue(title: str) -> dict[str, Any] | None:
    result = await _graphql_request(
        _FIND_OPEN_ISSUE,
        {
            "filter": {
                "team": {"key": {"eq": TEAM_KEY}},
                "title": {"eq": title},
                "state": {"type": {"nin": ["completed", "canceled"]}},
            }
        },
    )
    nodes = _require(result, "issues").get("nodes") or []
    return nodes[0] if nodes else None


async def _team_and_backlog_ids() -> tuple[str, str]:
    result = await _graphql_request(_TEAM_AND_STATE, {"key": TEAM_KEY, "state": "Backlog"})
    teams = _require(result, "teams").get("nodes") or []
    if not teams:
        raise RuntimeError(f"Linear team {TEAM_KEY} was not found")
    states = (teams[0].get("states") or {}).get("nodes") or []
    if not states:
        raise RuntimeError(f"Backlog state for Linear team {TEAM_KEY} was not found")
    return str(teams[0]["id"]), str(states[0]["id"])


async def _create_issue(
    title: str, description: str, *, subscriber_ids: list[str] | None = None
) -> dict[str, Any]:
    team_id, state_id = await _team_and_backlog_ids()
    issue_input: dict[str, Any] = {
        "teamId": team_id,
        "stateId": state_id,
        "title": title,
        "description": description,
    }
    if subscriber_ids is not None:
        issue_input["subscriberIds"] = subscriber_ids
    result = await _graphql_request(_CREATE_ISSUE, {"input": issue_input})
    created = _require(result, "issueCreate")
    if not created.get("success") or not isinstance(created.get("issue"), dict):
        raise RuntimeError(f"Linear did not create issue {title!r}")
    return created["issue"]


async def _comment(issue_id: str, body: str) -> None:
    result = await _graphql_request(_CREATE_COMMENT, {"issueId": issue_id, "body": body})
    if not _require(result, "commentCreate").get("success"):
        raise RuntimeError(f"Linear did not comment on issue {issue_id}")


def _provider_lines(providers: dict[str, dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for provider, verdict in providers.items():
        line = f"- **{provider}**: class `{verdict['class']}`; live call `{verdict['live_call']}`"
        if verdict.get("error"):
            line += f"; `{verdict['error']}`"
        lines.append(line)
    return lines


async def _write_alert(providers: dict[str, dict[str, str]], timestamp: str) -> None:
    failures = {
        name: verdict for name, verdict in providers.items() if verdict["status"] == "failed"
    }
    detail_lines = _provider_lines(failures)
    body = "\n".join(
        [
            f"Subscription OAuth health check failed at {timestamp}.",
            "",
            *detail_lines,
            "",
            "Recovery: see `OPERATIONS.md` § Subscription OAuth.",
        ]
    )
    issue = await _find_open_issue(ALERT_TITLE)
    if issue is None:
        issue = await _create_issue(
            ALERT_TITLE,
            "\n".join(
                [
                    "Nightly subscription OAuth degradation alert.",
                    "",
                    "Recovery: see `OPERATIONS.md` § Subscription OAuth.",
                ]
            ),
        )
    await _comment(str(issue["id"]), body)


async def _subscriber_id() -> str:
    result = await _graphql_request(_USER_BY_EMAIL, {"email": HEARTBEAT_SUBSCRIBER_EMAIL})
    users = _require(result, "users").get("nodes") or []
    if not users:
        raise RuntimeError(f"Linear user {HEARTBEAT_SUBSCRIBER_EMAIL} was not found")
    return str(users[0]["id"])


async def _write_heartbeat(
    status: str, providers: dict[str, dict[str, str]], timestamp: str
) -> None:
    issue = await _find_open_issue(HEARTBEAT_TITLE)
    if issue is None:
        subscriber_id = await _subscriber_id()
        issue = await _create_issue(
            HEARTBEAT_TITLE,
            "Nightly subscription OAuth status heartbeat. See `OPERATIONS.md` § Subscription OAuth.",
            subscriber_ids=[subscriber_id],
        )
    lines = [f"- UTC: `{timestamp}`", f"- Overall status: **{status}**"]
    if status == "disabled":
        lines.append("- Subscription auth: `disabled`")
    else:
        lines.extend(_provider_lines(providers))
    await _comment(str(issue["id"]), "\n".join(lines))


async def check_subscription_auth() -> dict[str, Any]:
    """Construct and call both OAuth models, then publish alert and heartbeat status."""
    timestamp = datetime.now(UTC).isoformat()
    if not _enabled():
        try:
            await _write_heartbeat("disabled", {}, timestamp)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write subscription-auth heartbeat")
        return {"status": "disabled"}

    providers: dict[str, dict[str, str]] = {}
    for provider, (model_id, expected_class) in _PROVIDER_MODELS.items():
        verdict = {"status": "failed", "class": "not constructed", "live_call": "not run"}
        try:
            model = subscription_model(model_id, {"max_tokens": 1})
            observed_class = type(model).__name__
            verdict["class"] = observed_class
            if model is None or observed_class != expected_class:
                verdict["error"] = f"expected {expected_class}"
            else:
                await model.ainvoke("Reply OK")
                verdict.update(status="healthy", live_call="succeeded")
        except Exception as exc:  # noqa: BLE001
            verdict["error"] = f"{type(exc).__name__}: {exc}"
            verdict["live_call"] = "failed"
        providers[provider] = verdict

    status = (
        "failed" if any(item["status"] == "failed" for item in providers.values()) else "healthy"
    )
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
