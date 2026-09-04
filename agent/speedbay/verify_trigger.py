"""Completion-verification trigger for Linear issue-state webhooks (OPE-39).

SPEEDBAY org-layer file — upstream does not own it.

When a Linear issue *enters* the ``ready-for-verify`` workflow state (routed
there by the per-team GitHub merge automation configured in OPE-38), this
module dispatches one sandboxed agent run that verifies the merged PR against
the issue's acceptance criteria and writes exactly one evidence-backed
``done``/``incomplete`` verdict back to Linear. This replaces the pi-forge
symphony daemon's ready-for-verify poll with an event-driven trigger.

Wiring: ``agent/webhooks/linear_routes.py`` calls :func:`maybe_handle` right
after signature verification and JSON parsing (marked ``SPEEDBAY
REGISTRATION``, listed in FORK.md). ``None`` means "not a verify transition —
continue with upstream's comment handling"; a dict is the route's response.

Duplicate-delivery safety is structural, not best-effort: verified live,
every Linear event is delivered once per covering webhook (two for OPE), and
both deliveries carry the same ``data.updatedAt``. A bounded process-local
per-issue transition map claims one identity before dispatch, releases failed
dispatches for retry, and suppresses stale delayed deliveries. The deterministic
per-issue thread id plus the prompt's re-read-before-finalize rule are the
backstop for anything this map cannot see (multi-worker deployments, process
restarts). A later re-entry into ready-for-verify (incomplete → rework → merge)
carries a new ``updatedAt`` and resumes the same thread with prior verification
context.

Loop safety: the verdict transition targets ``done``/``incomplete``, which
never match the ready-for-verify filter, and verdict comments are authored by
the runtime key, which the OPE-23 guard drops from the comment trigger. As
belt and braces, self-authored state changes are dropped here too.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks
from langgraph_sdk.schema import MultitaskStrategy

from ..webhooks import common
from . import linear_guard
from .config import trigger_owner_emails

logger = logging.getLogger(__name__)

# Workflow-state name that queues completion verification. Protocol shared
# with the Linear git automations (OPE-38), not an operational knob — a
# rename is a workflow change across every team, not a per-instance tuning.
VERIFY_STATE_NAME = "ready-for-verify"

_PROMPT_PATH = Path(__file__).parent / "resources" / "verify_prompt.md"
_prompt_cache: str | None = None


def _verify_contract() -> str:
    """The static verification contract, read once per process."""
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def is_verify_transition(payload: dict[str, Any]) -> bool:
    """Whether a webhook payload is an issue *entering* ready-for-verify.

    Pinned to a captured live delivery (tests/speedbay/
    linear_issue_update_payload.json): ``type == "Issue"``, ``action ==
    "update"``, ``updatedFrom`` carries the previous ``stateId`` (so the state
    actually changed — mere edits while sitting in ready-for-verify carry no
    ``stateId``), and the new state's name is exactly ``ready-for-verify``.
    """
    if payload.get("type") != "Issue" or payload.get("action") != "update":
        return False
    updated_from = payload.get("updatedFrom") or {}
    if "stateId" not in updated_from:
        return False
    state = (payload.get("data") or {}).get("state") or {}
    return state.get("name") == VERIFY_STATE_NAME


# Process-local delivery state. Multi-worker deployments and restarts still rely
# on the deterministic thread and re-read-before-finalize backstop.
_SEEN_MAX = 512


@dataclass
class _TransitionRecord:
    identity: tuple[str, str]
    watermark: datetime
    state: Literal["pending", "succeeded", "retryable"]


_transition_records: dict[str, _TransitionRecord] = {}
_RFC3339_OFFSET = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")


def _parse_updated_at(updated_at: Any) -> datetime | None:
    """Parse an RFC3339 timestamp for ordering, preserving raw equality elsewhere."""
    if (
        not isinstance(updated_at, str)
        or not updated_at.strip()
        or not _RFC3339_OFFSET.search(updated_at)
    ):
        return None
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _admit_transition(issue_id: str, updated_at: str, watermark: datetime) -> bool:
    """Claim a valid transition synchronously; False leaves it untracked."""
    if issue_id in _transition_records:
        _transition_records[issue_id] = _TransitionRecord(
            (issue_id, updated_at), watermark, "pending"
        )
        return True
    if len(_transition_records) >= _SEEN_MAX:
        for state in ("succeeded", "retryable"):
            evictable = next(
                (key for key, record in _transition_records.items() if record.state == state), None
            )
            if evictable is not None:
                del _transition_records[evictable]
                break
    if len(_transition_records) >= _SEEN_MAX:
        return False
    _transition_records[issue_id] = _TransitionRecord((issue_id, updated_at), watermark, "pending")
    return True


async def _process_transition_delivery(issue_data: dict[str, Any]) -> bool:
    """Claim, dispatch, and settle one webhook delivery identity."""
    issue_id = issue_data.get("id")
    updated_at = issue_data.get("updatedAt")
    watermark = _parse_updated_at(updated_at)
    if not isinstance(issue_id, str) or not isinstance(updated_at, str) or watermark is None:
        return await process_verify_dispatch(issue_data)

    identity = (issue_id, updated_at)
    current = _transition_records.get(issue_id)
    tracked = False
    if current is None:
        tracked = _admit_transition(issue_id, updated_at, watermark)
    elif current.identity == identity:
        if current.state in {"pending", "succeeded"}:
            return False
        current.state = "pending"
        tracked = True
    elif watermark <= current.watermark:
        return False
    else:
        tracked = _admit_transition(issue_id, updated_at, watermark)

    if not tracked:
        return False

    try:
        dispatched = await process_verify_dispatch(issue_data)
    except Exception:
        if tracked and _transition_records.get(issue_id) is not None:
            current = _transition_records[issue_id]
            if current.identity == identity:
                current.state = "retryable"
        raise
    if tracked and _transition_records.get(issue_id) is not None:
        current = _transition_records[issue_id]
        if current.identity == identity:
            current.state = "succeeded" if dispatched else "retryable"
    return dispatched


async def _assignee_email(issue_id: str) -> str | None:
    """The issue assignee's email, or None (unassigned or lookup failure)."""
    from ..utils.linear import get_issue

    result = await get_issue(issue_id)
    issue = result.get("issue") if isinstance(result, dict) else None
    assignee = (issue or {}).get("assignee") or {}
    email = assignee.get("email")
    return email if isinstance(email, str) and email.strip() else None


async def _is_foreign_issue(issue_id: str) -> bool:
    """OPE-36 trigger-owner scoping for verify transitions.

    Comment triggers scope by the comment author, but a ready-for-verify
    transition is authored by the shared merge automation — its actor never
    identifies the owning operator. The stable owner signal is the issue
    **assignee** (the same key forge's daemon used via FORGE_DAEMON_OWNER).
    Unscoped instances accept every transition and skip the lookup entirely.
    Scoped instances act only when the assignee's email matches an owner
    email, and fail closed for unassigned issues or lookup failures — better
    an unverified issue a human notices than two instances publishing rival
    verdicts.
    """
    owners = trigger_owner_emails()
    if not owners:
        return False
    email = await _assignee_email(issue_id)
    if email is None:
        logger.info(
            "verify trigger-owner scope: issue %s has no resolvable assignee email — dropping",
            issue_id,
        )
        return True
    return email.strip().lower() not in owners


async def maybe_handle(
    payload: dict[str, Any], background_tasks: BackgroundTasks
) -> dict[str, str] | None:
    """Handle a verify transition, or return None for upstream handling.

    Called by the Linear webhook route after signature verification. Fast
    checks run inline; issue fetching, repo resolution, and dispatch run as a
    background task so the webhook responds immediately.
    """
    if not is_verify_transition(payload):
        return None
    data = payload.get("data") or {}
    identifier = data.get("identifier", "") or data.get("id", "")
    # is_self_comment checks the id carriers shared by all payload types
    # (actor.id / data.userId), so it guards Issue events too.
    if await linear_guard.is_self_comment(payload):
        logger.info("Ignoring verify transition on %s: authored by the runtime key", identifier)
        return {"status": "ignored", "reason": "Transition authored by the runtime Linear key"}
    if not data.get("id"):
        return {"status": "ignored", "reason": "Issue payload has no id"}
    logger.info("Verify transition accepted for %s", identifier)
    background_tasks.add_task(_process_transition_delivery, data)
    return {"status": "accepted", "trigger": "verify-completion", "issue": identifier}


async def _verdict_state_ids(*team_ids: str) -> dict[str, str]:
    """Resolve the team's ``done``/``incomplete`` workflow-state ids.

    The agent's Linear tools cannot list team workflow states
    (``linear_update_issue`` needs an already-known ``state_id``), so the ids
    are resolved server-side here and injected into the prompt. Returns a
    possibly-partial mapping; the prompt tells the agent what to do when an
    id is missing (post the verdict comment, leave the state, and say so).
    """
    from ..utils.linear import _graphql_request

    team_id = next((t for t in team_ids if t), "")
    if not team_id:
        return {}
    try:
        result = await _graphql_request(
            "query($teamId: String!) { team(id: $teamId) { states { nodes { id name type } } } }",
            {"teamId": team_id},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to resolve verdict state ids for team %s", team_id)
        return {}
    nodes = (((result or {}).get("team") or {}).get("states") or {}).get("nodes") or []
    wanted = {"done", "incomplete"}
    return {s["name"]: s["id"] for s in nodes if s.get("name") in wanted and s.get("id")}


def _build_prompt(
    full_issue: dict[str, Any],
    repo_config: dict[str, str],
    verdict_states: dict[str, str],
) -> str:
    """The run prompt: issue context first, then the verification contract."""
    identifier = full_issue.get("identifier", "")
    title = full_issue.get("title", "No title")
    description = full_issue.get("description") or "No description"
    url = full_issue.get("url", "")
    issue_id = full_issue.get("id", "")
    if verdict_states:
        states_line = ", ".join(
            f"`{name}` = `{sid}`" for name, sid in sorted(verdict_states.items())
        )
    else:
        states_line = "(could not be resolved server-side)"
    return (
        f"Verify completion of the following Linear issue:\n\n"
        f"## Repository: {repo_config.get('owner')}/{repo_config.get('name')}\n\n"
        f"## Linear Ticket: {identifier} - Ticket ID: {issue_id}\n\n"
        f"## Linear Ticket URL: {url}\n\n"
        f"## Verdict workflow state ids for this issue's team: {states_line}\n\n"
        f"## Title: {title}\n\n"
        f"## Description:\n{description}\n\n"
        f"{_verify_contract()}"
    )


async def process_verify_dispatch(
    issue_data: dict[str, Any], *, multitask_strategy: MultitaskStrategy = "interrupt"
) -> bool:
    """Resolve repo + issue context and dispatch the verification run.

    Mirrors upstream's ``process_linear_issue`` dispatch shape (thread owner
    metadata, ``dispatch_agent_run``, trace comment) but with a distinct
    deterministic thread id — ``verify:<issue-id>`` — so verification never
    interrupts the issue's implementation thread, and with the verification
    contract as the prompt instead of the implementation prompt. The conflict
    strategy defaults to webhook follow-up behavior; the sweep passes
    ``"reject"`` so it cannot interrupt a live verifier. Returns whether a run
    was dispatched.
    """
    issue_id = issue_data.get("id", "")
    if await _is_foreign_issue(issue_id):
        return False
    full_issue = await common.fetch_linear_issue_details(issue_id) or issue_data

    # Repo resolution precedence: issue-body ``repo:owner/name`` declaration
    # (OPE-49) > team/project mapping > team default. Verify dispatches carry
    # no comment, so the comment-text override can never apply here; the
    # allowlist below still gates the body-declared repo.
    description = full_issue.get("description") or issue_data.get("description") or ""
    team = full_issue.get("team") or issue_data.get("team") or {}
    project = full_issue.get("project") or {}
    repo_config = (
        common.extract_repo_from_text(description, allow_github_url=False) if description else None
    )
    if not repo_config:
        repo_config = common.get_repo_config_from_team_mapping(
            (team.get("name") or "").strip(), (project.get("name") or "").strip()
        )
    if not repo_config:
        repo_config = await common.get_team_default_repo()
    if not repo_config:
        logger.warning("Verify dispatch for %s dropped: no repository configured", issue_id)
        return False
    if not common._is_repo_allowed(repo_config):
        logger.warning(
            "Verify dispatch for %s dropped: repo %s/%s not in allowlist",
            issue_id,
            repo_config.get("owner"),
            repo_config.get("name"),
        )
        return False

    identifier = full_issue.get("identifier", "") or issue_data.get("identifier", "")
    thread_id = common.generate_thread_id_from_issue(f"verify:{issue_id}")
    # OPE-48: resolve the assignee email unconditionally so the thread-owner
    # upsert below can attribute the verify thread to them (the dashboard's
    # _owner_search_filters matches github_login / triggering_user_email).
    # Lookup failures and unassigned issues yield None and dispatch unchanged.
    assignee_email = await _assignee_email(issue_id)
    configurable: dict[str, Any] = {
        "repo": repo_config,
        "linear_issue": {
            "id": issue_id,
            "title": full_issue.get("title", ""),
            "url": full_issue.get("url", ""),
            "identifier": identifier,
            "linear_project_id": identifier.split("-", 1)[0] if "-" in identifier else "",
            "linear_issue_number": identifier.split("-", 1)[1] if "-" in identifier else "",
            "triggering_user_name": "",
        },
        # Run auth boundary (OPE-48): this must stay None. Setting it would
        # resolve the assignee's GitHub OAuth token for the run (per-user
        # token resolution); verification runs under the bot identity. The
        # assignee attribution lives only in the thread-owner metadata below.
        "user_email": None,
        "source": "linear",
    }

    await common.upsert_agent_thread_owner_metadata(
        thread_id,
        source="linear",
        repo_config=repo_config,
        github_login="",
        # Pass the email only — the upsert resolves the login internally via
        # resolve_login_from_email_async; never resolve it here (OPE-48).
        user_email=assignee_email or "",
        title=f"Verify: {identifier or issue_id}",
        source_context={"linear_issue": configurable["linear_issue"]},
    )
    verdict_states = await _verdict_state_ids((team.get("id") or ""), issue_data.get("teamId", ""))
    run = await common.dispatch_agent_run(
        thread_id,
        _build_prompt(full_issue, repo_config, verdict_states),
        configurable,
        source="linear",
        metadata=common._AGENT_VERSION_METADATA,
        multitask_strategy=multitask_strategy,
    )
    # Deliberately no post_linear_trace_comment: the verifier's write-back
    # contract is one Linear comment — the verdict — and nothing else.
    logger.info(
        "Verification run dispatched for %s (thread=%s run=%s)",
        identifier or issue_id,
        thread_id,
        run.get("run_id") if isinstance(run, dict) else None,
    )
    return True
