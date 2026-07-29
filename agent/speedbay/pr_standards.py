"""Atomicity-cap and commit-hygiene gate before ``open_pull_request`` (OPE-8).

SPEEDBAY org-layer file — upstream does not own it. Before a PR opens, diff
the requested head against the PR base inside the sandbox (``git diff
--numstat``), apply the OPE-14 rule content (``forge_rules`` — this module
adds no rules), and block with a corrective ToolMessage on any violation.
Interception mirrors ``pr_creation_guard`` / ``quality_gates`` (OPE-9): the
PR gate runs on the async path (``open_pull_request`` is async-only); the
shell-fallback check (upstream's ``is_pr_creation_fallback_command`` — ``gh
pr create`` / ``gh api`` POST / ``curl``, incl. ``||``, pipe, and nested-shell
forms) runs on both paths so nothing routes around the gate.

Fail-open for infrastructure problems only (no thread id, unreachable
sandbox, undiffable base), per quality_gates; rule verdicts always block, and
a truncated numstat blocks as oversized (missing rows would undercount).
Corrective rounds are bounded: after ``_MAX_CORRECTIVE_ROUNDS`` blocks on one
thread the message flips to escalation (``recoverable_by_agent: false``); the
human approval-pause wiring lands in OPE-10.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

import attrs
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ..middleware.pr_creation_guard import is_pr_creation_fallback_command
from ..utils.sandbox_state import get_sandbox_backend
from .forge_rules.atomicity import check_atomicity, parse_numstat
from .forge_rules.hygiene import check_attribution, check_hygiene
from .quality_gates import _tool_args, _tool_call_id, _tool_name

logger = logging.getLogger(__name__)

_WORKSPACE = "/workspace"
_DIFF_TIMEOUT_SECONDS = 120
_MAX_CORRECTIVE_ROUNDS = 3

_FALLBACK_ERROR = (
    "New pull requests must be opened with the open_pull_request tool so the "
    "PR standards gate (atomicity caps, commit hygiene) run before the PR exists. "
    "Do not fall back to gh pr create, gh api, or curl."
)


async def _numstat(backend: Any, base: str, head: str) -> tuple[str, bool] | None:
    """``git diff --numstat`` of ``head`` vs the PR base, or None if undiffable.

    Mirrors quality_gates ``_changed_paths``: tries ``origin/<base>`` (the push
    target) then ``<base>``; refs are shell-quoted (model-controlled args).
    Returns ``(output, truncated)`` — truncated output is missing rows.
    """
    for ref in (f"origin/{base}", base):
        response = await backend.aexecute(
            f"cd {_WORKSPACE} && git diff --numstat {shlex.quote(ref)}...{shlex.quote(head)}",
            timeout=_DIFF_TIMEOUT_SECONDS,
        )
        if getattr(response, "exit_code", None) == 0:
            output = getattr(response, "output", "") or ""
            return output, bool(getattr(response, "truncated", False))
    return None


def _issue_id(configurable: dict[str, Any]) -> str | None:
    """The triggering Linear issue identifier (e.g. ``OPE-8``), or None."""
    linear_issue = configurable.get("linear_issue") or {}
    identifier = linear_issue.get("identifier") if isinstance(linear_issue, dict) else None
    return identifier if isinstance(identifier, str) and identifier.strip() else None


class PRStandardsMiddleware(AgentMiddleware):
    """Block non-compliant ``open_pull_request`` calls and shell PR fallbacks."""

    state_schema = AgentState

    def __init__(self) -> None:
        super().__init__()
        # ponytail: in-memory per-process round counter keyed by thread id;
        # OPE-10 moves escalation state to the durable approval pause.
        self._rounds: dict[str, int] = {}

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
        return gate_block if gate_block is not None else await handler(request)

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
            diff = await _numstat(backend, base, branch)
            if diff is None:
                logger.warning("PR standards gate: could not diff against base %r — passing", base)
                return None
            numstat, truncated = diff
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
            self._rounds.pop(str(thread_id), None)
            return None

        rounds = self._rounds.get(str(thread_id), 0) + 1
        self._rounds[str(thread_id)] = rounds
        escalate = rounds >= _MAX_CORRECTIVE_ROUNDS
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
        if escalate:
            advice.append(
                f"This was corrective round {rounds} of {_MAX_CORRECTIVE_ROUNDS}: stop "
                "retrying and surface the gate failure to a human for approval."
            )
        logger.info(
            "PR standards gate: blocking open_pull_request (round %d) — atomicity=%s hygiene=%s",
            rounds,
            verdict.exceeded,
            [v.rule for v in violations],
        )
        content = {
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
        return ToolMessage(
            content=json.dumps(content), tool_call_id=_tool_call_id(request), status="error"
        )
