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

from ..utils.linear import _graphql_request
from ..utils.thread_ops import langgraph_client
from ..webhooks import common
from . import verify_trigger
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
    nodes { id identifier updatedAt team { id name key } }
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
    ``updatedAt`` is older than the cutoff, skips those with a busy verify
    thread, and dispatches the rest with server-side conflict rejection so a
    race cannot interrupt a run. Per-issue work is wrapped so one bad issue
    never aborts the sweep.

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
