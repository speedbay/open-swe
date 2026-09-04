"""Reconciliation sweep for missed ready-for-verify issues (OPE-42).

SPEEDBAY org-layer file — upstream does not own it.

The OPE-39 verify trigger is event-driven and missed events do not replay: an
issue transitioned into ``ready-for-verify`` while the backend is down or
mid-deploy sits there forever. This sweep is the safety net — the scheduler
graph runs it hourly (``configurable.task == "verify_sweep"``), it finds
issues parked in ``ready-for-verify`` past a cutoff with no in-flight verify
run, and re-dispatches each through the same ``process_verify_dispatch`` path
the webhook uses, so assignee owner-scoping and prompt construction stay
enforced in exactly one place.

The webhook remains the fast path; this sweep only bounds missed-event
latency (cutoff + cron period). The cron is ensured idempotently at boot by
``agent/speedbay/verify_sweep_cron.py`` (OPE-53).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..utils.linear import _graphql_request, get_issue_comments
from ..utils.thread_ops import langgraph_client
from ..webhooks import common
from . import linear_guard, verify_trigger
from .config import verify_sweep_min_age_seconds

logger = logging.getLogger(__name__)

_SEARCH_PAGE_SIZE = 50

_STALE_ISSUES_QUERY = """
query StaleVerifyIssues($cutoff: DateTimeOrDuration!, $after: String) {
  issues(
    filter: {state: {name: {eq: "ready-for-verify"}}, updatedAt: {lt: $cutoff}}
    first: 50
    after: $after
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      identifier
      updatedAt
      team { id name key }
      stateHistory(last: 1) { nodes { startedAt endedAt state { name } } }
    }
  }
}
"""


async def _stale_verify_issues(cutoff_iso: str) -> list[dict[str, Any]]:
    """All issues parked in ready-for-verify since before ``cutoff_iso``."""
    issues: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        result = await _graphql_request(_STALE_ISSUES_QUERY, {"cutoff": cutoff_iso, "after": after})
        page = (result or {}).get("issues")
        if not isinstance(page, dict):
            logger.warning("Stale-issue query failed: %s", (result or {}).get("error"))
            return issues
        issues.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return issues
        after = info.get("endCursor")


def _current_ready_for_verify_started_at(issue: dict[str, Any]) -> str:
    """The durable start time of the issue's current verify state span."""
    spans = (issue.get("stateHistory") or {}).get("nodes")
    if not isinstance(spans, list) or len(spans) != 1:
        raise ValueError("missing current state span")
    span = spans[0]
    if not isinstance(span, dict):
        raise ValueError("malformed current state span")
    started_at = span.get("startedAt")
    if (
        span.get("endedAt") is not None
        or (span.get("state") or {}).get("name") != "ready-for-verify"
        or not isinstance(started_at, str)
    ):
        raise ValueError("current state span is not ready-for-verify")
    datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    return started_at


def _has_current_terminal_verdict(
    comments: list[Any], started_at: str, runtime_user_id: str
) -> bool:
    """Whether runtime-authored comments contain one contract-valid current verdict."""
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("malformed comment")
        created_at = comment.get("createdAt")
        body = comment.get("body")
        author = comment.get("user")
        if (
            not isinstance(created_at, str)
            or not isinstance(body, str)
            or not isinstance(author, dict)
            or not isinstance(author.get("id"), str)
        ):
            raise ValueError("malformed comment")
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        lines = body.splitlines()
        verdicts = [line for line in lines if line.startswith("Verdict:")]
        if (
            author["id"] == runtime_user_id
            and created >= started
            and "## Completion verification" in lines
            and verdicts in (["Verdict: done"], ["Verdict: incomplete"])
        ):
            return True
    return False


async def _verify_thread_busy(issue_id: str) -> bool:
    """Whether the issue's verify thread currently has an in-flight run.

    A missing thread (404) means no verify run was ever dispatched — not busy.
    Errors report busy (fail-closed): skipping one sweep tick is cheaper than
    interrupting a live verification.
    """
    thread_id = common.generate_thread_id_from_issue(f"verify:{issue_id}")
    client = langgraph_client()
    try:
        thread = await client.threads.get(thread_id)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status_code", None) == 404:
            return False
        logger.warning("Could not inspect verify thread for %s: %s", issue_id, exc)
        return True
    status = thread.get("status") if isinstance(thread, dict) else getattr(thread, "status", None)
    return status == "busy"


async def sweep_stale_verify_issues(*, min_age_seconds: int | None = None) -> dict[str, int]:
    """Re-dispatch verification for issues stuck in ready-for-verify.

    Walks every issue whose state is ``ready-for-verify`` and whose
    ``updatedAt`` is older than the cutoff, skips busy verify threads and
    current-cycle terminal reports, then dispatches the rest with server-side
    conflict rejection so a race cannot interrupt a run. Per-issue work is
    wrapped so one bad issue never aborts the sweep.

    Returns counts: ``{"checked", "skipped_busy", "dispatched", "errors"}``.
    """
    age = verify_sweep_min_age_seconds() if min_age_seconds is None else min_age_seconds
    cutoff_iso = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()

    checked = skipped_busy = dispatched = errors = 0
    for issue in await _stale_verify_issues(cutoff_iso):
        checked += 1
        identifier = issue.get("identifier", "") or issue.get("id", "")
        try:
            if not issue.get("id"):
                continue
            if await _verify_thread_busy(issue["id"]):
                logger.info("Sweep skipping %s: verify thread is busy", identifier)
                skipped_busy += 1
                continue
            comments_result = await get_issue_comments(issue["id"])
            if not isinstance(comments_result, dict) or "error" in comments_result:
                raise ValueError("could not read issue comments")
            comments = comments_result.get("comments")
            if not isinstance(comments, list):
                raise ValueError("malformed issue comments")
            runtime_user_id = await linear_guard._viewer_id()
            if not runtime_user_id:
                raise ValueError("could not resolve runtime Linear user")
            if _has_current_terminal_verdict(
                comments, _current_ready_for_verify_started_at(issue), runtime_user_id
            ):
                logger.info("Sweep skipping %s: current cycle has a terminal verdict", identifier)
                continue
            logger.info("Sweep re-dispatching verification for %s", identifier)
            try:
                did_dispatch = await verify_trigger.process_verify_dispatch(
                    issue, multitask_strategy="reject"
                )
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) == 409:
                    logger.info("Sweep skipping %s: verify run won dispatch race", identifier)
                    skipped_busy += 1
                    continue
                raise
            if did_dispatch:
                dispatched += 1
        except Exception:  # noqa: BLE001
            logger.exception("Sweep failed for %s; continuing", identifier)
            errors += 1

    counts = {
        "checked": checked,
        "skipped_busy": skipped_busy,
        "dispatched": dispatched,
        "errors": errors,
    }
    logger.info("verify_sweep complete: %s", counts)
    return counts
