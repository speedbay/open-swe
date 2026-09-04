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

Fail-open for infrastructure problems only **before a trustworthy rule
verdict exists** (no thread id, unreachable sandbox, undiffable base), per
quality_gates; rule verdicts always block, and a truncated numstat blocks as
oversized (missing rows would undercount).

The gate's response is split by the rule's declared severity
(``rules/findings.py``, OPE-75):

- **HARD (atomicity)** — the first violation is an immediate human decision
  point (no corrective-round budget): the middleware records a durable
  pending approval **before** posting the Linear breach comment itself
  (post-once via ``notified``, error-level log on failure), then ends the
  active run — the blocking ToolMessage (``recoverable_by_agent: false``) is
  returned through ``Command(update={"messages": [...]}, goto=END)`` and the
  ``before_model`` hook jumps the loop to end, so no subsequent model turn
  runs. A durable-store fault **after** a rule verdict also fails closed and
  ends the run with explicit infrastructure evidence. A dashboard approve
  grants a one-time, fingerprint-bound exemption; ``consume_gate_approval``
  spends it on the next gate attempt, and a new commit re-gates. A reject
  leaves the run ended.
- **REMEDIABLE (hygiene)** — PR-metadata and commit-message fixes are mechanical
  and preserve file content, so a hygiene-only failure returns an agent-recoverable
  corrective ToolMessage embedding the required format: no approval card, no
  Linear post, the run continues. Budgeted at ``HYGIENE_REMEDIATION_ATTEMPTS``
  failed attempts per thread; exhaustion escalates the run through the HARD
  path (disposition ``escalated_after_remediation_budget``) — severity
  classification itself never mutates. A mixed failure (both families)
  resolves to the stricter response immediately.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import attrs
from langchain.agents.middleware.types import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall
from langgraph.config import get_config
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

from ..middleware.pr_creation_guard import is_pr_creation_fallback_command
from ..utils.linear import comment_on_linear_issue
from ..utils.sandbox_state import get_sandbox_backend

# Tunable settings live in config.py (OPE-31); both gates share the sandbox
# workspace root and diff timeout through it.
from .config import DIFF_TIMEOUT_SECONDS, HYGIENE_REMEDIATION_ATTEMPTS, WORKSPACE
from .gate_approval import (
    GATE_APPROVAL_APPROVED,
    GATE_APPROVAL_CONSUMED,
    GATE_APPROVAL_PENDING,
    GATE_APPROVAL_REJECTED,
    consume_gate_approval,
    dashboard_gate_approval_url,
    ensure_gate_approval_pending,
    gate_approval_status,
    gate_fingerprint,
    mark_gate_approval_notified,
    pr_metadata_digest,
)
from .quality_gates import _tool_args, _tool_call_id, _tool_name, resolve_repo_dir
from .rules.atomicity import atomicity_findings, check_atomicity, parse_numstat
from .rules.findings import GateFinding, GateSeverity
from .rules.hygiene import (
    Violation,
    check_commit_message,
    check_hygiene,
    check_subject_independent,
    hygiene_findings,
)

logger = logging.getLogger(__name__)

_EVIDENCE_TAIL_CHARS = 2000

# Block-message codes that end the active run. The shell-fallback block and
# the hygiene corrective-retry block are deliberately absent: both leave the
# run alive so the agent can retry via open_pull_request.
_HALT_CODES = frozenset({"pr_standards_failed", "pr_standards_store_error"})

# Run dispositions reported in the block payload. Disposition is middleware
# policy (what happened to this run); finding severity is the rule's
# intrinsic classification and never mutates (rules/findings.py).
DISPOSITION_HUMAN_DECISION = "human_decision"
DISPOSITION_AGENT_REMEDIATION = "agent_remediation"
DISPOSITION_ESCALATED = "escalated_after_remediation_budget"

# The literal contract the agent must satisfy, embedded in every hygiene
# corrective block so remediation is one-shot (the same contract already
# rides the system prompt via SpeedbayConventionsMiddleware).
_HYGIENE_FORMAT = (
    "Required format — title or commit subject: '<TEAM>-NNN: <imperative subject>' "
    "using the triggering issue id; branch: '<team>-nnn-<slug>'; PR or commit body: "
    "'Closes <TEAM>-NNN' before the first heading, then exactly these sections in "
    "order, each non-empty: '## Why needed', '## Solved / fixed', "
    "'## Workflow enabled / fixed', '## Verification'; no AI attribution anywhere."
)

_FALLBACK_ERROR = (
    "New pull requests must be opened with the open_pull_request tool so the "
    "PR standards gate (atomicity caps, commit hygiene) run before the PR exists. "
    "Do not fall back to gh pr create, gh api, or curl."
)


async def _numstat(
    backend: Any, base: str, head: str, repo_dir: str
) -> tuple[str, bool, str, str] | None:
    """``git diff --numstat`` of ``head`` vs the PR base, or None if undiffable.

    Runs inside ``repo_dir`` — the resolved repo clone under ``/workspace``
    (see quality_gates ``resolve_repo_dir``), never ``/workspace`` itself,
    which is not a git repo (OPE-59). Refs are resolved to immutable SHAs
    **first** and the diff runs against those SHAs, so the evaluated diff and
    the approval fingerprint cannot diverge — a ref moving between operations
    is harmless because only pinned SHAs are ever diffed. Base resolution
    tries ``origin/<base>`` (the push target) then ``<base>``, falling back
    when the ref is missing or its SHA shares no merge base with ``head``.
    Refs and the directory are shell-quoted (model- and issue-controlled).
    Returns ``(output, truncated, base_sha, head_sha)`` — truncated output is
    missing rows.
    """
    head_sha = await _rev_parse(backend, repo_dir, head)
    if not head_sha:
        return None
    for ref in (f"origin/{base}", base):
        base_sha = await _rev_parse(backend, repo_dir, ref)
        if not base_sha:
            continue
        response = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} diff --numstat "
            f"{shlex.quote(base_sha)}...{shlex.quote(head_sha)}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if getattr(response, "exit_code", None) == 0:
            output = getattr(response, "output", "") or ""
            return output, bool(getattr(response, "truncated", False)), base_sha, head_sha
    return None


async def _commit_messages(
    backend: Any, repo_dir: str, base_sha: str, head_sha: str
) -> tuple[tuple[str, str], ...] | None:
    """Return branch commit SHAs and full messages, or None when git log fails."""
    try:
        response = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} log --format=%H%x1f%B%x1e "
            f"{shlex.quote(base_sha)}..{shlex.quote(head_sha)}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if getattr(response, "exit_code", None) != 0 or getattr(response, "truncated", False):
        return None
    output = getattr(response, "output", "") or ""
    if not output:
        return ()
    records = output.split("\x1e")
    if records and not records[-1].strip():
        records.pop()
    commits: list[tuple[str, str]] = []
    for record in records:
        sha, separator, message = record.lstrip("\n").partition("\x1f")
        if not separator or not sha.strip():
            return None
        commits.append((sha.strip(), message.rstrip("\n")))
    return tuple(commits)


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


async def _upstream_sync_commits(
    backend: Any,
    repo_dir: str,
    base_sha: str,
    head_sha: str,
    commit_shas: frozenset[str],
    *,
    enabled: bool,
) -> frozenset[str]:
    """Return only range commits proven reachable from a fetched upstream main."""
    if not enabled:
        return frozenset()
    ref = "refs/openswe/hygiene-upstream-main"
    try:
        fetch = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} fetch --no-tags "
            f"https://github.com/langchain-ai/open-swe.git main:{ref}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if getattr(fetch, "exit_code", None) != 0:
            return frozenset()
        upstream_sha = await _rev_parse(backend, repo_dir, ref)
        if not upstream_sha:
            return frozenset()
        graph = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} rev-list --parents "
            f"{shlex.quote(base_sha)}..{shlex.quote(head_sha)}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if getattr(graph, "exit_code", None) != 0 or getattr(graph, "truncated", False):
            return frozenset()
        merge_parents = [
            line.split()[2]
            for line in (getattr(graph, "output", "") or "").splitlines()
            if len(line.split()) > 2
        ]
        sync_merge = False
        for parent in merge_parents:
            if await _is_ancestor(backend, repo_dir, parent, upstream_sha):
                sync_merge = True
                break
        if not sync_merge:
            return frozenset()
        proven: set[str] = set()
        for commit in commit_shas:
            if await _is_ancestor(backend, repo_dir, commit, upstream_sha):
                proven.add(commit)
        return frozenset(proven)
    except Exception:
        logger.exception("PR standards gate: upstream provenance verification failed")
    return frozenset()


async def _is_ancestor(backend: Any, repo_dir: str, commit: str, target: str) -> bool:
    response = await backend.aexecute(
        f"git -C {shlex.quote(repo_dir)} merge-base --is-ancestor "
        f"{shlex.quote(commit)} {shlex.quote(target)}",
        timeout=DIFF_TIMEOUT_SECONDS,
    )
    return getattr(response, "exit_code", None) == 0


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
    findings: tuple[GateFinding, ...],
    disposition: str,
    code: str = "pr_standards_failed",
    recoverable: bool = False,
    fingerprint: str | None = None,
    approval_url: str | None = None,
    approval_status: str | None = None,
) -> ToolMessage:
    """Gate-block ToolMessage. ``findings`` severities always report the rules'
    intrinsic classification, even when the run's ``disposition`` escalated."""
    content: dict[str, Any] = {
        "status": "error",
        "error_type": "PRStandardsFailed",
        "code": code,
        "recoverable_by_agent": recoverable,
        "error": " ".join(advice),
        "disposition": disposition,
        "atomicity": {
            "passed": verdict.passed,
            "raw_loc": verdict.raw_loc,
            "effective_loc": verdict.effective_loc,
            "production_files": verdict.production_files,
            "exceeded": list(verdict.exceeded),
        },
        "findings": [
            {
                "domain": f.domain,
                "rule": f.rule,
                "message": f.message,
                "severity": f.severity.value,
            }
            for f in findings
        ],
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


def _halt(message: ToolMessage) -> Command:
    """End the active run after recording ``message`` as the tool result.

    The ``Command`` carries the matching ToolMessage (required by
    ``ToolNode._validate_tool_command`` for a current-graph update) and
    ``goto=END``. The agent loop's tools→model conditional edge can still
    route past a tool-level goto, so ``before_model`` (below) is the
    enforcement backstop: it jumps to end whenever the last message is one
    of these halt blocks — no subsequent model turn runs either way.
    """
    return Command(update={"messages": [message]}, goto=END)


def _is_halt_message(message: Any) -> bool:
    """True when ``message`` is a PR-standards halt block (see ``_HALT_CODES``)."""
    if not isinstance(message, ToolMessage):
        return False
    try:
        content = json.loads(str(message.content))
    except (TypeError, ValueError):
        return False
    return isinstance(content, dict) and content.get("code") in _HALT_CODES


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
        "The agent's pull request failed the PR-standards gate and the run has "
        "ended pending a human decision.",
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
            "commit re-gates. Rejecting keeps the run ended; rework or split the "
            "ticket with the pi-forge planning skills.",
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
        # ponytail: in-process hygiene remediation counter — a backend restart
        # resets it, which only re-grants remediation attempts; the HARD path
        # and its durable records are unaffected. Durable counting would
        # resurrect the rounds machinery OPE-75 removed.
        self._hygiene_failures: dict[str, int] = {}

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

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        """End the run when the last message is a PR-standards halt block.

        The enforcement backstop for ``_halt``: a tool-level ``goto=END`` does
        not override the agent loop's tools→model conditional edge, so this
        hook (the ``sandbox_circuit_breaker`` precedent) jumps to end before
        any subsequent model turn. An approval follow-up appends a new human
        message, so resumed threads never match here.
        """
        messages = state.get("messages", [])
        if messages and _is_halt_message(messages[-1]):
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        """Async mirror of ``before_model`` (the agent runs on the async path)."""
        return self.before_model(state, runtime)

    async def _gate_open_pull_request(
        self, request: ToolCallRequest
    ) -> Command | ToolMessage | None:
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
            numstat, truncated, base_sha, head_sha = diff
            parse_failure: str | None = None
            try:
                verdict = check_atomicity(parse_numstat(numstat))
            except ValueError as exc:
                parse_failure = str(exc)
                verdict = check_atomicity([])
                verdict = attrs.evolve(verdict, passed=False, exceeded=(parse_failure,))
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
            messages = await _commit_messages(backend, repo_dir, base_sha, head_sha)
            if messages is None:
                logger.error(
                    "PR standards gate: could not inspect commits for %r — commit hygiene passing",
                    repo_dir,
                )
                commit_violations = ()
            else:
                repo = configurable.get("repo")
                upstream_commits = await _upstream_sync_commits(
                    backend,
                    repo_dir,
                    base_sha,
                    head_sha,
                    frozenset(sha for sha, _ in messages),
                    enabled=(
                        isinstance(repo, dict)
                        and repo.get("owner") == "speedbay"
                        and repo.get("name") == "open-swe"
                    ),
                )
                commit_violations = tuple(
                    Violation(f"commit-{violation.rule}", f"commit {sha[:12]}: {violation.message}")
                    for sha, message in messages
                    for violation in check_commit_message(
                        message,
                        issue_id,
                        exempt_rules=(
                            frozenset({"title-format", "closes-line", "ai-attribution"})
                            if sha in upstream_commits
                            else frozenset()
                        ),
                    )
                )
            if issue_id is not None:
                violations = (*check_hygiene(title, body, branch, issue_id), *commit_violations)
            else:
                logger.info("PR standards gate: no Linear issue in config — universal hygiene only")
                violations = (*check_subject_independent(f"{title}\n{body}"), *commit_violations)
        except Exception:
            # Infrastructure fault in the gate itself must not permanently
            # block PR creation — log loudly and fail open (OPE-9 precedent).
            logger.exception("PR standards gate: gate infrastructure error — passing")
            return None

        findings = (*atomicity_findings(verdict), *hygiene_findings(violations))
        if not findings:
            self._hygiene_failures.pop(str(thread_id), None)
            return None

        hard = any(f.severity is GateSeverity.HARD for f in findings)
        failed_rule_ids = ["atomicity"] * (not verdict.passed) + [v.rule for v in violations]
        advice: list[str] = []
        if not verdict.passed:
            if parse_failure:
                advice.append(
                    "Atomicity numstat parsing failed: "
                    + parse_failure
                    + ". Inspect and correct the git diff --numstat output before retrying."
                )
            else:
                advice.append(
                    "Atomicity caps exceeded: " + "; ".join(verdict.exceeded) + ". Split the "
                    "change into smaller, independently reviewable PRs (one vertical slice "
                    "each)."
                )
        if violations:
            advice.append(
                "Commit hygiene violations: "
                + "; ".join(f"[{v.rule}] {v.message}" for v in violations)
                + "."
            )

        if not hard:
            # REMEDIABLE-only (hygiene): agent-correctable in-run, budgeted.
            attempts = self._hygiene_failures.get(str(thread_id), 0) + 1
            self._hygiene_failures[str(thread_id)] = attempts
            if attempts < HYGIENE_REMEDIATION_ATTEMPTS:
                logger.info(
                    "PR standards gate: hygiene corrective retry (attempt %d/%d) — %s",
                    attempts,
                    HYGIENE_REMEDIATION_ATTEMPTS,
                    [f.rule for f in findings],
                )
                advice.append(_HYGIENE_FORMAT)
                if any(v.rule.startswith("commit-") for v in violations):
                    advice.append(
                        "Amend or reword each named commit message, then push the corrected "
                        "history and retry open_pull_request; do not change the file diff."
                    )
                else:
                    advice.append(
                        "Fix the PR title/body/branch and retry open_pull_request; do not "
                        "amend commits or change the diff."
                    )
                advice.append(
                    f"This is failed attempt {attempts} of "
                    f"{HYGIENE_REMEDIATION_ATTEMPTS}; on the last failure the run "
                    "pauses for human approval."
                )
                return _block_message(
                    request,
                    advice=advice,
                    verdict=verdict,
                    findings=findings,
                    disposition=DISPOSITION_AGENT_REMEDIATION,
                    code="pr_standards_hygiene_retry",
                    recoverable=True,
                )
            advice.append(
                f"Hygiene remediation budget exhausted ({attempts} failed attempts) — "
                "escalating to human approval."
            )

        return await self._durable_block(
            request,
            thread_id=str(thread_id),
            backend=backend,
            repo_dir=repo_dir,
            base_sha=base_sha,
            head_sha=head_sha,
            issue_id=issue_id,
            issue_uuid=_issue_uuid(configurable),
            verdict=verdict,
            findings=findings,
            disposition=DISPOSITION_HUMAN_DECISION if hard else DISPOSITION_ESCALATED,
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
        base_sha: str,
        head_sha: str,
        issue_id: str | None,
        issue_uuid: str | None,
        verdict: Any,
        findings: tuple[GateFinding, ...],
        disposition: str,
        failed_rule_ids: list[str],
        metadata_digest: str,
        evidence: str,
        advice: list[str],
    ) -> Command | None:
        """First-violation halt: durable pending record, post-once notify, run end.

        Returns None only when a one-time approved exemption is consumed — the
        caller then treats the gate as passed for this exact diff. Every other
        outcome (fresh breach, still-pending breach, rejection, durable-store
        fault) ends the active run via ``_halt``; there is no corrective-round
        budget (OPE-75).
        """
        try:
            # The SHAs arrive from _numstat, which resolved them before
            # diffing — the fingerprint binds exactly the evaluated diff.
            fingerprint = gate_fingerprint(base_sha, head_sha, failed_rule_ids, metadata_digest)
            approval_url = dashboard_gate_approval_url(thread_id, fingerprint)
            diff_stats = {
                "raw_loc": verdict.raw_loc,
                "effective_loc": int(verdict.effective_loc),
                "production_files": verdict.production_files,
                "exceeded": list(verdict.exceeded),
            }
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
            # This status read is the freshest view after the lock-serialized
            # ensure call: a rejection landing in any earlier window (there is
            # deliberately no pre-ensure terminal read to race against) is
            # caught here instead of pausing the run on an approval that can
            # never arrive. ensure() returns terminal records unchanged, so a
            # rejected fingerprint was never rewritten to pending.
            status = await gate_approval_status(thread_id, fingerprint)
            if status == GATE_APPROVAL_REJECTED:
                return _halt(
                    _block_message(
                        request,
                        advice=[
                            "A human rejected this gate breach and the run has ended. "
                            "A human must rework or split the ticket with the "
                            "pi-forge planning skills; do not retry "
                            "open_pull_request with this diff."
                        ],
                        verdict=verdict,
                        findings=findings,
                        disposition=DISPOSITION_HUMAN_DECISION,
                        fingerprint=fingerprint,
                        approval_url=approval_url,
                        approval_status=GATE_APPROVAL_REJECTED,
                    )
                )
            if status == GATE_APPROVAL_APPROVED:
                if await consume_gate_approval(thread_id, fingerprint):
                    # The human decision supersedes earlier failed attempts:
                    # an exhausted hygiene budget must not leak into the next
                    # remediable defect on this thread (PR #54 review).
                    self._hygiene_failures.pop(thread_id, None)
                    logger.info(
                        "PR standards gate: passing thread %s on a one-time approved "
                        "exemption (fingerprint %s)",
                        thread_id,
                        fingerprint,
                    )
                    return None  # exemption consumed: gate passes this diff once
                # Lost the consume race: a concurrent attempt spent the
                # exemption between the status read and the consume. Re-read
                # and report the real state instead of claiming a pending
                # approval that does not exist (PR #54 review).
                status = await gate_approval_status(thread_id, fingerprint)
                if status == GATE_APPROVAL_CONSUMED:
                    return _halt(
                        _block_message(
                            request,
                            advice=[
                                "The one-time exemption for this exact diff was "
                                "already consumed by a concurrent attempt, so no "
                                "pending approval exists and the run has ended. A "
                                "new non-compliant attempt would open a fresh "
                                "pending approval."
                            ],
                            verdict=verdict,
                            findings=findings,
                            disposition=disposition,
                            fingerprint=fingerprint,
                            approval_url=approval_url,
                            approval_status=GATE_APPROVAL_CONSUMED,
                        )
                    )
            # First violation — the durable record exists before any
            # notification attempt; then the run ends for a human decision.
            posted = await _notify_gate_escalation(thread_id, fingerprint, record, created=created)
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
                "The run has ended pending human approval"
                + (f" at {approval_url}" if approval_url else "")
                + f". Do not retry open_pull_request — {surfacing}"
            )
            logger.info(
                "PR standards gate: halting open_pull_request (%s) — %s",
                disposition,
                [f.rule for f in findings],
            )
            return _halt(
                _block_message(
                    request,
                    advice=advice,
                    verdict=verdict,
                    findings=findings,
                    disposition=disposition,
                    fingerprint=fingerprint,
                    approval_url=approval_url,
                    approval_status=GATE_APPROVAL_PENDING,
                )
            )
        except Exception:
            # A durable-store fault after a rule verdict fails CLOSED (OPE-75):
            # permitting PR creation or another corrective turn on a store
            # outage would bypass the human decision point entirely.
            logger.exception("PR standards gate: durable approval state error — run ends")
            failed_advice = list(advice)
            failed_advice.append(
                "The durable gate-approval state store failed (see backend logs "
                "for the stack trace), so no dashboard approval record could be "
                "created. The run has ended without opening a PR; a human must "
                "fix the approval store and start a new run."
            )
            return _halt(
                _block_message(
                    request,
                    advice=failed_advice,
                    verdict=verdict,
                    findings=findings,
                    disposition=disposition,
                    code="pr_standards_store_error",
                )
            )
