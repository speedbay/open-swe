"""Atomicity-cap and commit-hygiene gate before ``open_pull_request`` (OPE-8).

SPEEDBAY org-layer file — upstream does not own it. Before a PR opens, diff
the requested head against the PR base inside the sandbox (``git diff
--numstat``), apply the OPE-14 rule content (``rules`` — this module
adds no rules), and block with a corrective ToolMessage on any violation.
Interception mirrors ``pr_creation_guard`` / ``quality_gates`` (OPE-9): the
PR gate runs on the async path (``open_pull_request`` is async-only); the
shell-fallback check (upstream's ``is_pr_creation_fallback_command`` — ``gh
pr create`` / ``gh api`` POST / ``curl``, incl. ``||``, pipe, and nested-shell
forms) runs on both paths so nothing routes around the gate.

Fail-open for infrastructure problems only (no thread id, unreachable
sandbox, undiffable base), per quality_gates; rule verdicts always block, and
a truncated numstat blocks as oversized (missing rows would undercount).
Corrective rounds are bounded: the round counter is durable (per-fingerprint
record in thread metadata, ``gate_approval.bump_gate_rounds`` — survives
backend restarts), and after ``MAX_CORRECTIVE_ROUNDS`` blocks on one diff the
run pauses for human approval (OPE-10): the middleware records a durable
pending approval **before** posting the Linear escalation comment itself
(post-once via ``notified``, error-level log on failure), then returns a
blocking ToolMessage (``recoverable_by_agent: false``). A dashboard approve
grants a one-time, fingerprint-bound exemption; ``consume_gate_approval``
spends it on the next gate attempt, and a new commit re-gates.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import attrs
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ..middleware.pr_creation_guard import is_pr_creation_fallback_command
from ..utils.linear import comment_on_linear_issue
from ..utils.sandbox_state import get_sandbox_backend

# Tunable settings live in config.py (OPE-31); both gates share the sandbox
# workspace root and diff timeout through it.
from .config import DIFF_TIMEOUT_SECONDS, MAX_CORRECTIVE_ROUNDS, WORKSPACE
from .gate_approval import (
    GATE_APPROVAL_APPROVED,
    GATE_APPROVAL_PENDING,
    GATE_APPROVAL_REJECTED,
    bump_gate_rounds,
    consume_gate_approval,
    dashboard_gate_approval_url,
    ensure_gate_approval_pending,
    gate_approval_status,
    gate_fingerprint,
    get_gate_approvals,
    mark_gate_approval_notified,
    pr_metadata_digest,
)
from .quality_gates import _tool_args, _tool_call_id, _tool_name, resolve_repo_dir
from .rules.atomicity import check_atomicity, parse_numstat
from .rules.hygiene import check_attribution, check_hygiene

logger = logging.getLogger(__name__)

_EVIDENCE_TAIL_CHARS = 2000

_FALLBACK_ERROR = (
    "New pull requests must be opened with the open_pull_request tool so the "
    "PR standards gate (atomicity caps, commit hygiene) run before the PR exists. "
    "Do not fall back to gh pr create, gh api, or curl."
)


async def _numstat(
    backend: Any, base: str, head: str, repo_dir: str
) -> tuple[str, bool, str] | None:
    """``git diff --numstat`` of ``head`` vs the PR base, or None if undiffable.

    Runs inside ``repo_dir`` — the resolved repo clone under ``/workspace``
    (see quality_gates ``resolve_repo_dir``), never ``/workspace`` itself,
    which is not a git repo (OPE-59). Mirrors quality_gates ``_changed_paths``:
    tries ``origin/<base>`` (the push target) then ``<base>``; refs and the
    directory are shell-quoted (model- and issue-controlled args).
    Returns ``(output, truncated, base_ref)`` — truncated output is missing
    rows, and ``base_ref`` is the ref the diff actually ran against so the
    approval fingerprint binds the same base the verdict was computed from
    (never a differently-resolved fallback).
    """
    for ref in (f"origin/{base}", base):
        response = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} diff --numstat "
            f"{shlex.quote(ref)}...{shlex.quote(head)}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if getattr(response, "exit_code", None) == 0:
            output = getattr(response, "output", "") or ""
            return output, bool(getattr(response, "truncated", False)), ref
    return None


async def _rev_parse(backend: Any, repo_dir: str, ref: str) -> str | None:
    """Resolve ``ref`` to a full SHA inside the sandbox, or None."""
    try:
        response = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} rev-parse {shlex.quote(ref)}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if getattr(response, "exit_code", None) != 0:
        return None
    output = (getattr(response, "output", "") or "").strip().splitlines()
    sha = output[0].strip() if output else ""
    return sha or None


async def _base_and_head_shas(
    backend: Any, base_ref: str, head: str, repo_dir: str
) -> tuple[str, str] | None:
    """The SHAs the fingerprint binds; None when unresolvable (fail open).

    ``base_ref`` is the exact ref ``_numstat`` diffed against — resolved
    directly, with no independent origin/local fallback, so the fingerprint
    can never bind a different base than the evaluated diff.
    """
    head_sha = await _rev_parse(backend, repo_dir, head)
    base_sha = await _rev_parse(backend, repo_dir, base_ref)
    if not head_sha or not base_sha:
        return None
    return base_sha, head_sha


def _force_ready_for_review(request: ToolCallRequest) -> ToolCallRequest:
    """Copy a gate-passing ``open_pull_request`` request with ``draft: False``.

    Upstream defaults the tool to draft PRs and its prompt reinforces that;
    Speed Bay's flow expects ready-for-review PRs (OPE-34). Deterministic
    arg rewrite — the model cannot opt back into drafts. Uses the immutable
    ``request.override(...)`` pattern with copied dicts: ``request.tool_call``
    is the same dict stored in the ``AIMessage`` conversation history, so
    mutating it in place would corrupt the persisted tool-call record.
    """
    tool_call = getattr(request, "tool_call", None)
    if not (isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict)):
        return request
    ready = {**tool_call, "args": {**tool_call["args"], "draft": False}}
    return request.override(tool_call=cast("ToolCall", ready))


def _issue_id(configurable: dict[str, Any]) -> str | None:
    """The triggering Linear issue identifier (e.g. ``OPE-8``), or None."""
    linear_issue = configurable.get("linear_issue") or {}
    identifier = linear_issue.get("identifier") if isinstance(linear_issue, dict) else None
    return identifier if isinstance(identifier, str) and identifier.strip() else None


def _issue_uuid(configurable: Mapping[str, Any]) -> str | None:
    """The triggering Linear issue UUID — the id ``comment_on_linear_issue`` needs."""
    linear_issue = configurable.get("linear_issue") or {}
    uuid = linear_issue.get("id") if isinstance(linear_issue, dict) else None
    return uuid if isinstance(uuid, str) and uuid.strip() else None


def _evidence_tail(numstat: str) -> str:
    return numstat[-_EVIDENCE_TAIL_CHARS:] if numstat else ""


def _block_message(
    request: ToolCallRequest,
    *,
    advice: list[str],
    verdict: Any,
    violations: tuple[Any, ...],
    rounds: int,
    escalate: bool,
    fingerprint: str | None = None,
    approval_url: str | None = None,
    approval_status: str | None = None,
) -> ToolMessage:
    content: dict[str, Any] = {
        "status": "error",
        "error_type": "PRStandardsFailed",
        "code": "pr_standards_failed",
        "recoverable_by_agent": not escalate,
        "error": " ".join(advice),
        "atomicity": {
            "passed": verdict.passed,
            "raw_loc": verdict.raw_loc,
            "effective_loc": verdict.effective_loc,
            "production_files": verdict.production_files,
            "exceeded": list(verdict.exceeded),
        },
        "hygiene_violations": [{"rule": v.rule, "message": v.message} for v in violations],
        "corrective_round": rounds,
        "escalation_required": escalate,
    }
    if fingerprint is not None:
        content["gate_approval"] = {
            "fingerprint": fingerprint,
            "status": approval_status,
            "approval_url": approval_url,
        }
    return ToolMessage(
        content=json.dumps(content), tool_call_id=_tool_call_id(request), status="error"
    )


def _gate_escalation_linear_comment(
    record: dict[str, Any],
    *,
    thread_id: str,
) -> str:
    raw_stats: Any = record.get("diff_stats")
    stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
    exceeded = stats.get("exceeded") if isinstance(stats.get("exceeded"), list) else []
    failed = record.get("failed_rule_ids")
    failed = [str(rule) for rule in failed] if isinstance(failed, list) else []
    approval_url = record.get("approval_url")
    issue_label = record.get("issue_identifier") or record.get("issue_id") or ""
    heading = "**PR-standards gate breach — human approval required**"
    if isinstance(issue_label, str) and issue_label:
        heading = f"**PR-standards gate breach on {issue_label} — human approval required**"
    lines = [
        heading,
        "",
        "The agent could not satisfy the PR gates within "
        f"{MAX_CORRECTIVE_ROUNDS} corrective rounds and the run is paused.",
        "",
        f"- Raw LOC: {stats.get('raw_loc', 0)} · effective LOC: "
        f"{stats.get('effective_loc', 0)} · production files: "
        f"{stats.get('production_files', 0)}",
    ]
    if exceeded:
        lines.append(f"- Exceeded caps: {'; '.join(str(item) for item in exceeded)}")
    if failed:
        lines.append(f"- Failed rules: {', '.join(f'`{rule}`' for rule in failed)}")
    lines.append(f"- Fingerprint: `{record.get('fingerprint')}` (thread `{thread_id}`)")
    if isinstance(approval_url, str) and approval_url:
        lines.extend(["", f"Approve or reject: {approval_url}"])
    lines.extend(
        [
            "",
            "Approving grants a one-time exemption for exactly this diff — any new "
            "commit re-gates. Rejecting ends the escalation; split the change instead.",
        ]
    )
    return "\n".join(lines)


async def _notify_gate_escalation(
    thread_id: str,
    fingerprint: str,
    record: dict[str, Any],
    *,
    created: bool = False,
) -> bool:
    """Post-once Linear surfacing; never blocks the durable record.

    Returns True when this breach cycle has a posted Linear comment (now or
    previously); False when no comment exists — the caller must not tell the
    agent that surfacing already happened.
    """
    # A freshly created record is always notified even if a stale store fault
    # left it with a stray `notified` flag (post-once for a given record).
    if record.get("notified") is True and not created:
        return True
    issue_id = record.get("issue_id")
    if not isinstance(issue_id, str) or not issue_id:
        logger.error(
            "gate approval: escalation for thread %s fingerprint %s has no Linear "
            "issue id — breach is only visible in the dashboard pending list",
            thread_id,
            fingerprint,
        )
        return False
    try:
        ok = await comment_on_linear_issue(
            issue_id, _gate_escalation_linear_comment(record, thread_id=thread_id)
        )
    except Exception:
        ok = False
    if not ok:
        logger.error(
            "gate approval: failed to post escalation comment to %s for thread %s "
            "(approval URL: %s) — the breach stays discoverable in the dashboard "
            "pending-approvals list",
            issue_id,
            thread_id,
            record.get("approval_url"),
        )
        return False
    # Bind the mark to this cycle: if the record was refreshed while the post
    # was in flight, the new cycle keeps its own un-notified state.
    requested_at = record.get("requested_at")
    await mark_gate_approval_notified(
        thread_id,
        fingerprint,
        requested_at=requested_at if isinstance(requested_at, str) else None,
    )
    return True


class PRStandardsMiddleware(AgentMiddleware):
    """Block non-compliant ``open_pull_request`` calls and shell PR fallbacks."""

    state_schema = AgentState

    def __init__(self) -> None:
        super().__init__()
        # In-process rounds fallback, used only when the durable thread-metadata
        # store faults (logged at error): keeps corrective rounds bounded for the
        # active run instead of jumping straight to escalation advice.
        self._fallback_rounds: dict[str, int] = {}

    def _fallback_block(self, request: ToolCallRequest) -> ToolMessage | None:
        if _tool_name(request) != "execute":
            return None
        command = _tool_args(request).get("command")
        if not isinstance(command, str) or not is_pr_creation_fallback_command(command):
            return None
        content = {
            "status": "error",
            "error_type": "PRStandardsFallbackBlocked",
            "code": "pr_standards_fallback_blocked",
            "recoverable_by_agent": False,
            "error": _FALLBACK_ERROR,
            "blocked_command": command,
        }
        return ToolMessage(
            content=json.dumps(content), tool_call_id=_tool_call_id(request), status="error"
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        # open_pull_request is async-only (see quality_gates), so the sync
        # path needs only the shell-fallback check.
        blocked = self._fallback_block(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        blocked = self._fallback_block(request)
        if blocked is not None:
            return blocked
        if _tool_name(request) != "open_pull_request":
            return await handler(request)
        gate_block = await self._gate_open_pull_request(request)
        if gate_block is not None:
            return gate_block
        return await handler(_force_ready_for_review(request))

    async def _gate_open_pull_request(self, request: ToolCallRequest) -> ToolMessage | None:
        try:
            args = _tool_args(request)
            raw_title, raw_body = args.get("title"), args.get("body")
            title = raw_title if isinstance(raw_title, str) else ""
            body = raw_body if isinstance(raw_body, str) else ""
            base = args.get("base")
            base = base if isinstance(base, str) and base else "main"
            head = args.get("head")
            # `head` may be "owner:branch"; the sandbox only knows the branch.
            branch = head.rpartition(":")[2] if isinstance(head, str) and head else ""
            branch = branch or "HEAD"

            configurable = get_config().get("configurable", {})
            thread_id = configurable.get("thread_id")
            if not thread_id:
                logger.warning("PR standards gate: no thread_id in run config — passing")
                return None
            backend = await get_sandbox_backend(str(thread_id))
            repo_dir = await resolve_repo_dir(backend, configurable)
            if repo_dir is None:
                logger.error(
                    "PR standards gate: no repo clone found under %s (declared: %r) — passing",
                    WORKSPACE,
                    (configurable.get("repo") or {}).get("name"),
                )
                return None
            diff = await _numstat(backend, base, branch, repo_dir)
            if diff is None:
                logger.error(
                    "PR standards gate: could not diff %r against base %r — passing",
                    repo_dir,
                    base,
                )
                return None
            numstat, truncated, base_ref = diff
            verdict = check_atomicity(parse_numstat(numstat))
            if truncated:  # missing rows would undercount — fail closed
                verdict = attrs.evolve(
                    verdict,
                    passed=False,
                    exceeded=(
                        *verdict.exceeded,
                        "numstat output was truncated by the sandbox — the diff "
                        "is far over any Track-A cap",
                    ),
                )

            issue_id = _issue_id(configurable)
            if issue_id is not None:
                violations = check_hygiene(title, body, branch, issue_id)
            else:
                # Non-Linear run: issue-anchored rules (title format, branch,
                # Closes line) have no expected id; attribution still applies.
                logger.info("PR standards gate: no linear issue in config — attribution check only")
                attribution = check_attribution(f"{title}\n{body}")
                violations = (attribution,) if attribution is not None else ()
        except Exception:
            # Infrastructure fault in the gate itself must not permanently
            # block PR creation — log loudly and fail open (OPE-9 precedent).
            logger.exception("PR standards gate: gate infrastructure error — passing")
            return None

        if verdict.passed and not violations:
            if isinstance(thread_id, str) and thread_id:
                self._fallback_rounds.pop(thread_id, None)
            return None

        failed_rule_ids = ["atomicity"] * (not verdict.passed) + [v.rule for v in violations]
        advice: list[str] = []
        if not verdict.passed:
            advice.append(
                "Atomicity caps exceeded: " + "; ".join(verdict.exceeded) + ". Split the "
                "change into smaller, independently reviewable PRs (one vertical slice "
                "each) and retry open_pull_request per slice."
            )
        if violations:
            advice.append(
                "Commit hygiene violations: "
                + "; ".join(f"[{v.rule}] {v.message}" for v in violations)
                + ". Fix the PR title/body/branch and retry."
            )
        return await self._durable_block(
            request,
            thread_id=str(thread_id),
            backend=backend,
            repo_dir=repo_dir,
            base=base_ref,
            branch=branch,
            issue_id=issue_id,
            issue_uuid=_issue_uuid(configurable),
            verdict=verdict,
            violations=violations,
            failed_rule_ids=[str(rule) for rule in failed_rule_ids],
            metadata_digest=pr_metadata_digest(title, body, branch),
            evidence=_evidence_tail(numstat),
            advice=advice,
        )

    async def _durable_block(
        self,
        request: ToolCallRequest,
        *,
        thread_id: str,
        backend: Any,
        repo_dir: str,
        base: str,
        branch: str,
        issue_id: str | None,
        issue_uuid: str | None,
        verdict: Any,
        violations: tuple[Any, ...],
        failed_rule_ids: list[str],
        metadata_digest: str,
        evidence: str,
        advice: list[str],
    ) -> ToolMessage | None:
        """Durable round counter + OPE-10 approval pause behind one fail-open seam.

        Returns None only when a one-time approved exemption is consumed — the
        caller then treats the gate as passed for this exact diff.
        """
        try:
            shas = await _base_and_head_shas(backend, base, branch, repo_dir)
            if shas is None:
                raise RuntimeError("could not resolve base/head SHAs")
            base_sha, head_sha = shas
            fingerprint = gate_fingerprint(base_sha, head_sha, failed_rule_ids, metadata_digest)
            terminal = (await get_gate_approvals(thread_id)).get(fingerprint, {})
            terminal_status = terminal.get("status")
            approval_url = dashboard_gate_approval_url(thread_id, fingerprint)
            diff_stats = {
                "raw_loc": verdict.raw_loc,
                "effective_loc": int(verdict.effective_loc),
                "production_files": verdict.production_files,
                "exceeded": list(verdict.exceeded),
            }
            if terminal_status == GATE_APPROVAL_REJECTED:
                return _block_message(
                    request,
                    advice=[
                        "A human rejected this gate breach. Split the change into "
                        "smaller PRs or fix the violations and open a new run; do "
                        "not retry open_pull_request with this diff."
                    ],
                    verdict=verdict,
                    violations=violations,
                    rounds=int(terminal.get("rounds") or 0),
                    escalate=True,
                    fingerprint=fingerprint,
                    approval_url=approval_url,
                    approval_status=terminal_status,
                )
            record, created = await ensure_gate_approval_pending(
                thread_id,
                fingerprint=fingerprint,
                issue_id=issue_uuid or issue_id,
                issue_identifier=issue_id,
                base_sha=base_sha,
                head_sha=head_sha,
                failed_rule_ids=failed_rule_ids,
                diff_stats=diff_stats,
                evidence_tail=evidence,
                approval_url=approval_url,
            )
            status = await gate_approval_status(thread_id, fingerprint)
            if status == GATE_APPROVAL_APPROVED and await consume_gate_approval(
                thread_id, fingerprint
            ):
                logger.info(
                    "PR standards gate: passing thread %s on a one-time approved "
                    "exemption (fingerprint %s)",
                    thread_id,
                    fingerprint,
                )
                return None  # exemption consumed: gate passes this diff once
            rounds = await bump_gate_rounds(thread_id, fingerprint)
            escalate = rounds >= MAX_CORRECTIVE_ROUNDS
            if escalate:
                # The durable record exists before any notification attempt.
                posted = await _notify_gate_escalation(
                    thread_id, fingerprint, record, created=created
                )
                surfacing = (
                    "a Linear comment has been posted by the gate itself, no "
                    "further surfacing is needed."
                    if posted
                    else "the gate could NOT post its Linear comment (see backend "
                    "logs); the breach is visible in the dashboard pending-approvals "
                    "list — surface this gate failure to a human via the source "
                    "channel."
                )
                advice.append(
                    f"This was corrective round {rounds} of {MAX_CORRECTIVE_ROUNDS}: "
                    "the run is blocked pending human approval"
                    + (f" at {approval_url}" if approval_url else "")
                    + f". Do not retry open_pull_request until a human approves the "
                    f"gate breach — {surfacing}"
                )
            logger.info(
                "PR standards gate: blocking open_pull_request (round %d) — atomicity=%s hygiene=%s",
                rounds,
                verdict.exceeded,
                [v.rule for v in violations],
            )
            return _block_message(
                request,
                advice=advice,
                verdict=verdict,
                violations=violations,
                rounds=rounds,
                escalate=escalate,
                fingerprint=fingerprint,
                approval_url=approval_url if escalate else None,
                approval_status=GATE_APPROVAL_PENDING if escalate else None,
            )
        except Exception:
            # A fault in the durable-state layer must not wedge PR creation —
            # log loudly and fall back to the advice-only corrective message.
            logger.exception("PR standards gate: durable approval state error")
            rounds = self._fallback_rounds.get(thread_id, 0) + 1
            self._fallback_rounds[thread_id] = rounds
            fallback_advice = list(advice)
            if rounds >= MAX_CORRECTIVE_ROUNDS:
                fallback_advice.append(
                    "The durable gate-approval state store failed (see backend "
                    "logs): surface this gate failure to a human for approval."
                )
            # Never escalate on this path: without a durable pending record
            # there is no fingerprint for a human to approve, so flipping
            # recoverable_by_agent to False would wedge the run permanently —
            # a store outage stays agent-recoverable per the fail-open policy.
            return _block_message(
                request,
                advice=fallback_advice,
                verdict=verdict,
                violations=violations,
                rounds=rounds,
                escalate=False,
            )
