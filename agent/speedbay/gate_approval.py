"""Durable gate-breach approval state (OPE-10).

SPEEDBAY org-layer file — upstream does not own it. Sibling of the
workflow-push approval store (``agent/dashboard/workflow_approval.py``),
with the same record shape guarantees: records live in thread metadata,
are created **before** any notification attempt, terminal statuses are
idempotent, and a ``notified`` flag makes the Linear surfacing post-once.
Unlike workflow approvals (consumed once per retry), a gate approval is a
**one-time** exemption: ``consume_gate_approval`` flips an ``approved``
record to ``exemption_consumed`` so the same fingerprint never passes
twice, and any new commit (changed head SHA) changes the fingerprint and
re-gates. The PR-standards round counter also lives here (``rounds`` in
the per-fingerprint record) so escalation state survives backend restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from langgraph_sdk import get_client

from ..utils.dashboard_links import dashboard_thread_url

GATE_APPROVALS_KEY = "gate_approvals"
GATE_APPROVAL_PENDING = "pending"
GATE_APPROVAL_APPROVED = "approved"
GATE_APPROVAL_REJECTED = "rejected"
GATE_APPROVAL_CONSUMED = "exemption_consumed"
_MAX_APPROVAL_RECORDS = 20
# Statuses the dashboard cannot decide again. CONSUMED is deliberately absent:
# a spent exemption's record is refreshed back to pending by the next
# non-compliant attempt on the same diff (rounds keep accumulating).
_TERMINAL_STATUSES = {GATE_APPROVAL_REJECTED}


# ponytail: per-thread in-process locks serialize every read-modify-write on a
# thread's approval map — the LangGraph metadata store has no compare-and-set,
# and the laptop deployment is a single asyncio process, so this closes the
# realistic race (concurrent tasks interleaving at awaits). Ceiling: multiple
# backend processes would need storage-level CAS/locking instead.
_thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def dashboard_gate_approval_url(thread_id: str, fingerprint: str) -> str | None:
    """Dashboard gate-approval URL for a thread/fingerprint.

    Lives here (org layer) rather than in upstream ``utils/dashboard_links.py``
    so the fork's merge contract holds; composes on the upstream
    ``dashboard_thread_url`` without editing it.
    """
    thread_url = dashboard_thread_url(thread_id)
    if not thread_url or not fingerprint:
        return thread_url
    return f"{thread_url}?gateApproval={quote(fingerprint, safe='')}"


def gate_fingerprint(base_sha: str, head_sha: str, failed_rule_ids: list[str]) -> str:
    """Fingerprint binding the exact diff (base + head) and the failed rules.

    Per ``WorkflowPushChange``: an approval covers exactly one diff — any
    new commit changes the head SHA and re-gates.
    """
    payload = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "failed_rule_ids": sorted(failed_rule_ids),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _approvals_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = metadata.get(GATE_APPROVALS_KEY) if metadata else None
    if not isinstance(raw, dict):
        return {}
    approvals: dict[str, dict[str, Any]] = {}
    for fingerprint, value in raw.items():
        if isinstance(fingerprint, str) and fingerprint and isinstance(value, dict):
            record = dict(value)
            record.setdefault("fingerprint", fingerprint)
            approvals[fingerprint] = record
    return approvals


async def get_gate_approvals(thread_id: str) -> dict[str, dict[str, Any]]:
    client = get_client()
    thread = await client.threads.get(thread_id)
    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    return _approvals_from_metadata(metadata if isinstance(metadata, dict) else None)


async def gate_approval_status(thread_id: str, fingerprint: str) -> str | None:
    record = (await get_gate_approvals(thread_id)).get(fingerprint)
    status = record.get("status") if record else None
    return str(status) if isinstance(status, str) else None


async def ensure_gate_approval_pending(
    thread_id: str,
    *,
    fingerprint: str,
    issue_id: str | None,
    issue_identifier: str | None = None,
    base_sha: str,
    head_sha: str,
    failed_rule_ids: list[str],
    diff_stats: Mapping[str, Any] | None = None,
    evidence_tail: str = "",
    approval_url: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Store a pending approval unless a terminal record already exists.

    A consumed record (spent one-time exemption) is refreshed back to
    ``pending`` with ``notified`` cleared, per the module contract: a new
    non-compliant attempt on the same diff starts a new approval cycle
    (rounds keep accumulating) and must be dashboard-discoverable again.
    """
    async with _thread_locks[thread_id]:
        approvals = await get_gate_approvals(thread_id)
        existing = approvals.get(fingerprint)
        if existing and existing.get("status") in _TERMINAL_STATUSES:
            return existing, False

        review_fields = {
            "issue_id": issue_id,
            "issue_identifier": issue_identifier,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "failed_rule_ids": [str(rule) for rule in failed_rule_ids],
            "diff_stats": _normalize_diff_stats(diff_stats),
            "evidence_tail": evidence_tail,
            "approval_url": approval_url,
        }
        if existing:
            record = {**existing, **review_fields}
            if record.get("status") == GATE_APPROVAL_CONSUMED:
                record["status"] = GATE_APPROVAL_PENDING
                record["notified"] = False
                record["requested_at"] = _now()
            approvals[fingerprint] = record
            await _save_approvals(thread_id, approvals)
            return record, False

        record = {
            "fingerprint": fingerprint,
            "status": GATE_APPROVAL_PENDING,
            **review_fields,
            "rounds": 0,
            "requested_at": _now(),
            "notified": False,
        }
        approvals[fingerprint] = record
        await _save_approvals(thread_id, approvals)
        return record, True


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_diff_stats(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Stored shape: raw_loc/effective_loc/production_files/exceeded (snake)."""
    value = value if isinstance(value, Mapping) else {}
    exceeded = value.get("exceeded")
    return {
        "raw_loc": _safe_int(value.get("raw_loc")),
        "effective_loc": _safe_int(value.get("effective_loc")),
        "production_files": _safe_int(value.get("production_files")),
        "exceeded": [str(item) for item in exceeded] if isinstance(exceeded, list) else [],
    }


def _diff_stats_response(value: Mapping[str, Any] | None) -> dict[str, Any]:
    stats = _normalize_diff_stats(value)
    return {
        "rawLoc": stats["raw_loc"],
        "effectiveLoc": stats["effective_loc"],
        "productionFiles": stats["production_files"],
        "exceeded": stats["exceeded"],
    }


async def bump_gate_rounds(thread_id: str, fingerprint: str) -> int:
    """Increment and return the durable corrective-round counter.

    Creates a rounds-only record when none exists yet (the pending record
    arrives at escalation); terminal records are left untouched.
    """
    async with _thread_locks[thread_id]:
        approvals = await get_gate_approvals(thread_id)
        record = approvals.get(fingerprint)
        if record and record.get("status") in _TERMINAL_STATUSES:
            return _safe_int(record.get("rounds"))
        record = {"fingerprint": fingerprint, **(record or {})}
        record["rounds"] = _safe_int(record.get("rounds")) + 1
        approvals[fingerprint] = record
        await _save_approvals(thread_id, approvals)
        return int(record["rounds"])


async def mark_gate_approval_notified(
    thread_id: str, fingerprint: str, *, requested_at: str | None = None
) -> None:
    """Flag the record as notified — but only for the cycle that was posted.

    ``requested_at`` identifies the approval cycle the caller actually
    notified: if the record was refreshed to a new cycle while the Linear
    post was in flight (consumed → pending resets ``requested_at``), the
    stale mark is dropped so the new cycle's own notification is not
    suppressed. ``None`` skips the guard (caller has no cycle identity).
    """
    async with _thread_locks[thread_id]:
        approvals = await get_gate_approvals(thread_id)
        record = approvals.get(fingerprint)
        if not record:
            return
        if requested_at is not None and record.get("requested_at") != requested_at:
            return
        record["notified"] = True
        record["notified_at"] = _now()
        approvals[fingerprint] = record
        await _save_approvals(thread_id, approvals)


async def decide_gate_approval(
    thread_id: str,
    fingerprint: str,
    *,
    approved: bool,
    actor: str,
) -> dict[str, Any] | None:
    """Decide a **pending** record; anything else returns None.

    Only ``pending`` is decidable: re-deciding an ``approved`` or
    ``exemption_consumed`` record would let the dashboard re-arm a spent
    one-time exemption without a new breach. A consumed record becomes
    decidable again only after the next non-compliant attempt refreshes it
    to pending (``ensure_gate_approval_pending``).
    """
    async with _thread_locks[thread_id]:
        approvals = await get_gate_approvals(thread_id)
        record = approvals.get(fingerprint)
        if not record or record.get("status") != GATE_APPROVAL_PENDING:
            return None
        record["status"] = GATE_APPROVAL_APPROVED if approved else GATE_APPROVAL_REJECTED
        record["decided_at"] = _now()
        record["decided_by"] = actor
        approvals[fingerprint] = record
        await _save_approvals(thread_id, approvals)
        return record


async def consume_gate_approval(thread_id: str, fingerprint: str) -> bool:
    """Atomically spend an approved exemption — passes exactly once.

    Serialized by the per-thread lock: two concurrent consumers cannot both
    observe ``approved`` and both succeed.
    """
    async with _thread_locks[thread_id]:
        approvals = await get_gate_approvals(thread_id)
        record = approvals.get(fingerprint)
        if not record or record.get("status") != GATE_APPROVAL_APPROVED:
            return False
        record["status"] = GATE_APPROVAL_CONSUMED
        record["consumed_at"] = _now()
        approvals[fingerprint] = record
        await _save_approvals(thread_id, approvals)
        return True


def gate_approval_response(record: Mapping[str, Any]) -> dict[str, Any]:
    requested_at = record.get("requested_at")
    decided_at = record.get("decided_at")
    decided_by = record.get("decided_by")
    approval_url = record.get("approval_url")
    issue_id = record.get("issue_id")
    failed_rule_ids = record.get("failed_rule_ids")
    diff_stats = record.get("diff_stats")
    return {
        "fingerprint": str(record.get("fingerprint") or ""),
        "status": str(record.get("status") or GATE_APPROVAL_PENDING),
        "issueId": str(issue_id) if isinstance(issue_id, str) and issue_id else None,
        "issueIdentifier": str(record.get("issue_identifier") or "") or None,
        "baseSha": str(record.get("base_sha") or ""),
        "headSha": str(record.get("head_sha") or ""),
        "failedRuleIds": [str(rule) for rule in failed_rule_ids]
        if isinstance(failed_rule_ids, list)
        else [],
        "diffStats": _diff_stats_response(diff_stats if isinstance(diff_stats, Mapping) else None),
        "evidenceTail": str(record.get("evidence_tail") or ""),
        "rounds": _safe_int(record.get("rounds")),
        "approvalUrl": approval_url if isinstance(approval_url, str) and approval_url else None,
        "requestedAt": requested_at if isinstance(requested_at, str) else None,
        "decidedAt": decided_at if isinstance(decided_at, str) else None,
        "decidedBy": decided_by if isinstance(decided_by, str) else None,
    }


def gate_approval_responses(approvals: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(approvals.values(), key=lambda r: str(r.get("requested_at", "")), reverse=True)
    return [gate_approval_response(record) for record in ordered]


async def list_pending_gate_approvals() -> list[dict[str, Any]]:
    """Every pending gate approval across all threads: ``{thread_id, record}`` rows.

    The cross-thread fallback surface: a failed Linear post or a backend
    restart still leaves every breach discoverable here (the workflow-push
    precedent lists per-thread only). Newest request first.
    """
    rows: list[dict[str, Any]] = []
    page_size = 100
    offset = 0
    while True:
        threads = await get_client().threads.search(metadata={}, limit=page_size, offset=offset)
        for thread in threads or []:
            if not isinstance(thread, Mapping):
                continue
            thread_id = thread.get("thread_id")
            metadata = thread.get("metadata")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            for record in _approvals_from_metadata(
                metadata if isinstance(metadata, Mapping) else None
            ).values():
                if record.get("status") == GATE_APPROVAL_PENDING:
                    rows.append({"thread_id": thread_id, "record": record})
        if not threads or len(threads) < page_size:
            break
        offset += page_size
    rows.sort(key=lambda row: str(row["record"].get("requested_at", "")), reverse=True)
    return rows


async def _save_approvals(thread_id: str, approvals: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(approvals.values(), key=lambda r: str(r.get("requested_at", "")))
    trimmed = ordered[-_MAX_APPROVAL_RECORDS:]
    await get_client().threads.update(
        thread_id=thread_id,
        metadata={GATE_APPROVALS_KEY: {str(r["fingerprint"]): r for r in trimmed}},
    )
