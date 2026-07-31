"""REST API for gate-breach (PR-standards escalation) approvals (OPE-10).

SPEEDBAY org-layer file — upstream does not own it. Mirrors
``workflow_approval_api.py``: owner-gated approve/reject per thread,
approve dispatches a follow-up run on the same thread via
``_dispatch_followup`` (the machine resume), reject posts an explanatory
Linear comment and leaves the run ended. Adds one thing the precedent
lacks: a cross-thread listing of pending gate approvals so a failed
Linear post or a backend restart leaves every breach discoverable from
the dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..speedbay.gate_approval import (
    decide_gate_approval,
    gate_approval_response,
    gate_approval_responses,
    get_gate_approvals,
    list_pending_gate_approvals,
)
from ..utils.linear import comment_on_linear_issue
from .oauth import require_same_origin_for_mutations, require_session
from .plan_api import _dispatch_followup, _thread_metadata
from .thread_api import _thread_is_readable, _user_owns_thread

logger = logging.getLogger(__name__)

gate_approval_router = APIRouter(
    prefix="/dashboard/api/gate-approval",
    tags=["gate-approval"],
    dependencies=[Depends(require_same_origin_for_mutations)],
)
_SESSION_DEP = Depends(require_session)


@gate_approval_router.get("/pending")
async def list_all_pending_gate_approvals(
    session: dict[str, Any] = _SESSION_DEP,
) -> dict[str, Any]:
    """Pending gate approvals across every thread the session user owns.

    The cross-thread fallback surface (the workflow-push precedent lists
    per-thread only): a failed Linear post or a backend restart still leaves
    every breach discoverable from the dashboard.
    """
    login, email = session["sub"], session.get("email")
    rows = await list_pending_gate_approvals()
    pending: list[dict[str, Any]] = []
    for row in rows:
        thread_id = row["thread_id"]
        metadata = await _thread_metadata(thread_id)
        if not _user_owns_thread(metadata, login, email):
            continue
        pending.append({"threadId": thread_id, **gate_approval_response(row["record"])})
    return {"approvals": pending}


@gate_approval_router.get("/{thread_id}")
async def list_gate_approvals(
    thread_id: str, session: dict[str, Any] = _SESSION_DEP
) -> dict[str, Any]:
    metadata = await _thread_metadata(thread_id)
    if not _thread_is_readable(metadata):
        raise HTTPException(404, "thread not found")
    is_owner = _user_owns_thread(metadata, session["sub"], session.get("email"))
    if not is_owner:
        raise HTTPException(403, "only the thread owner can view gate approvals")
    approvals = await get_gate_approvals(thread_id)
    return {
        "threadId": thread_id,
        "isOwner": is_owner,
        "approvals": gate_approval_responses(approvals),
    }


@gate_approval_router.post("/{thread_id}/{fingerprint}/approve")
async def approve_gate_breach(
    thread_id: str, fingerprint: str, session: dict[str, Any] = _SESSION_DEP
) -> dict[str, Any]:
    metadata = await _thread_metadata(thread_id)
    if not _user_owns_thread(metadata, session["sub"], session.get("email")):
        raise HTTPException(403, "only the thread owner can approve gate breaches")
    record = await decide_gate_approval(thread_id, fingerprint, approved=True, actor=session["sub"])
    if record is None:
        raise HTTPException(404, "gate approval not found or already decided")
    await _dispatch_followup(
        thread_id,
        metadata,
        "A human approved the PR-standards gate breach (one-time exemption for "
        "the current diff). Retry open_pull_request now with the same diff — do "
        "not amend commits or change the PR title/body before retrying.",
        plan_mode=False,
    )
    return {"status": "approved", "fingerprint": fingerprint}


@gate_approval_router.post("/{thread_id}/{fingerprint}/reject")
async def reject_gate_breach(
    thread_id: str, fingerprint: str, session: dict[str, Any] = _SESSION_DEP
) -> dict[str, Any]:
    metadata = await _thread_metadata(thread_id)
    if not _user_owns_thread(metadata, session["sub"], session.get("email")):
        raise HTTPException(403, "only the thread owner can reject gate breaches")
    record = await decide_gate_approval(
        thread_id, fingerprint, approved=False, actor=session["sub"]
    )
    if record is None:
        raise HTTPException(404, "gate approval not found or already decided")
    await _post_rejection_comment(record, actor=session["sub"])
    return {"status": "rejected", "fingerprint": fingerprint}


async def _post_rejection_comment(record: dict[str, Any], *, actor: str) -> None:
    issue_id = record.get("issue_id")
    if not isinstance(issue_id, str) or not issue_id:
        return
    raw_stats: Any = record.get("diff_stats")
    stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
    exceeded = stats.get("exceeded") if isinstance(stats.get("exceeded"), list) else []
    approval_url = record.get("approval_url")
    body = (
        f"PR-standards gate breach **rejected** by {actor}: the run stays ended and "
        "no exemption was granted. Split the change into smaller PRs or fix the "
        "hygiene violations and start a new run.\n\n"
        f"Fingerprint: `{record.get('fingerprint')}`\n"
        + (
            f"Approval record: {approval_url}\n"
            if isinstance(approval_url, str) and approval_url
            else ""
        )
        + (f"Exceeded caps: {'; '.join(str(item) for item in exceeded)}" if exceeded else "")
    )
    try:
        ok = await comment_on_linear_issue(issue_id, body)
    except Exception:
        ok = False
    if not ok:
        logger.error(
            "gate approval: failed to post rejection comment to %s (fingerprint %s)",
            issue_id,
            record.get("fingerprint"),
        )
