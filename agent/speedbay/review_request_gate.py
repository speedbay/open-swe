import logging
from typing import Any

from langgraph.config import get_config

from agent.tools.request_pr_review import request_pr_review as upstream_request_pr_review

logger = logging.getLogger(__name__)

HUMAN_REVIEW_REQUEST_SOURCES = frozenset({"slack", "github"})
REVIEW_REQUEST_POLICY_ERROR = (
    "PR reviews must be requested by humans via Slack or GitHub. "
    "Org PR review is owned by Macroscope (ADR-017); implementation runs must not request reviews."
)


async def request_pr_review(pr_url: str) -> dict[str, Any]:
    """Start a human-requested reviewer agent for a GitHub pull request URL."""
    configurable = get_config().get("configurable", {})
    source = configurable.get("source") or "agent"
    if source not in HUMAN_REVIEW_REQUEST_SOURCES:
        logger.warning(
            "Refused PR review request: source=%s pr_url=%s thread_id=%s",
            source,
            pr_url,
            configurable.get("thread_id", ""),
        )
        return {"success": False, "error": REVIEW_REQUEST_POLICY_ERROR}

    return await upstream_request_pr_review(pr_url)
