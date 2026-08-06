import logging
from importlib import import_module
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.speedbay import review_request_gate

upstream_review_request = import_module("agent.tools.request_pr_review")
PR_URL = "https://github.com/speedbay/warehouse/pull/923"


def set_config(monkeypatch: pytest.MonkeyPatch, configurable: dict[str, Any]) -> None:
    config = {"configurable": configurable}
    monkeypatch.setattr(review_request_gate, "get_config", lambda: config)
    monkeypatch.setattr(upstream_review_request, "get_config", lambda: config)


@pytest.mark.parametrize(
    ("configurable", "source"),
    [
        ({"source": "loop", "thread_id": "thread-123"}, "loop"),
        ({"source": "linear", "thread_id": "thread-123"}, "linear"),
        ({"thread_id": "thread-123"}, "agent"),
    ],
)
async def test_non_human_sources_are_refused_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configurable: dict[str, Any],
    source: str,
) -> None:
    set_config(monkeypatch, configurable)
    dispatcher = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(upstream_review_request, "trigger_pr_review_from_ref", dispatcher)
    caplog.set_level(logging.WARNING, logger=review_request_gate.logger.name)

    result = await review_request_gate.request_pr_review(PR_URL)

    assert result == {"success": False, "error": review_request_gate.REVIEW_REQUEST_POLICY_ERROR}
    dispatcher.assert_not_awaited()
    assert "requested by humans via Slack or GitHub" in result["error"]
    assert "Macroscope (ADR-017)" in result["error"]
    assert "implementation runs must not request reviews" in result["error"]
    assert caplog.messages == [
        f"Refused PR review request: source={source} pr_url={PR_URL} thread_id=thread-123"
    ]


@pytest.mark.parametrize("source", ["slack", "github"])
async def test_human_request_sources_dispatch_unchanged(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    set_config(
        monkeypatch,
        {
            "source": source,
            "github_login": "human",
            "github_user_id": 42,
            "slack_thread": {"channel_id": "C123", "thread_ts": "123.456"},
        },
    )
    dispatcher = AsyncMock(return_value={"success": True, "review_thread_id": "review-123"})
    monkeypatch.setattr(upstream_review_request, "trigger_pr_review_from_ref", dispatcher)

    result = await review_request_gate.request_pr_review(PR_URL)

    assert result == {"success": True, "review_thread_id": "review-123"}
    dispatcher.assert_awaited_once()
    assert dispatcher.await_args is not None
    pr_ref = dispatcher.await_args.args[0]
    assert (pr_ref.owner, pr_ref.repo, pr_ref.number) == ("speedbay", "warehouse", 923)
    assert dispatcher.await_args.kwargs == {
        "source": source,
        "github_login": "human",
        "github_user_id": 42,
        "slack_channel_id": "C123",
        "slack_thread_ts": "123.456",
    }
