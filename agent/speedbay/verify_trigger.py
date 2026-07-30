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
both deliveries carry the same ``data.updatedAt``. A process-lifetime seen-map
keyed on ``(issue id, updatedAt)`` drops the duplicates before dispatch, so
only one run is created per transition; the deterministic per-issue thread id
plus the prompt's re-read-before-finalize rule are the backstop for anything
the seen-map cannot see (multi-worker deployments, process restarts). A later
re-entry into ready-for-verify (incomplete → rework → merge) carries a new
``updatedAt`` and resumes the same thread with prior verification context.

Loop safety: the verdict transition targets ``done``/``incomplete``, which
never match the ready-for-verify filter, and verdict comments are authored by
the runtime key, which the OPE-23 guard drops from the comment trigger. As
belt and braces, self-authored state changes are dropped here too.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks

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


# Process-lifetime duplicate-delivery guard. Linear delivers each event once
# per covering webhook (verified live: identical ``data.updatedAt`` on every
# copy), and ``create_durable_run`` has no idempotency key — without this, a
# duplicate arriving mid-run interrupts it and one arriving after completion
# starts a second run. Bounded FIFO; multi-worker deployments fall back to the
# thread-level re-read-before-finalize backstop.
_SEEN_MAX = 512
_seen_transitions: dict[str, str] = {}


def _is_duplicate_delivery(issue_id: str, updated_at: str) -> bool:
    """True when this (issue, updatedAt) transition was already dispatched."""
    if not updated_at:
        return False  # cannot distinguish — let the thread-level backstop handle it
    if _seen_transitions.get(issue_id) == updated_at:
        return True
    _seen_transitions[issue_id] = updated_at
    while len(_seen_transitions) > _SEEN_MAX:
        _seen_transitions.pop(next(iter(_seen_transitions)))
    return False


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
    if _is_duplicate_delivery(data["id"], data.get("updatedAt", "")):
        logger.info("Ignoring duplicate verify delivery for %s", identifier)
        return {
            "status": "ignored",
            "reason": "Duplicate delivery of an already-dispatched transition",
        }
    logger.info("Verify transition accepted for %s", identifier)
    background_tasks.add_task(process_verify_dispatch, data)
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


async def process_verify_dispatch(issue_data: dict[str, Any]) -> bool:
    """Resolve repo + issue context and dispatch the verification run.

    Mirrors upstream's ``process_linear_issue`` dispatch shape (thread owner
    metadata, ``dispatch_agent_run``, trace comment) but with a distinct
    deterministic thread id — ``verify:<issue-id>`` — so verification never
    interrupts the issue's implementation thread, and with the verification
    contract as the prompt instead of the implementation prompt. Returns
    whether a run was dispatched.
    """
    issue_id = issue_data.get("id", "")
    if await _is_foreign_issue(issue_id):
        return False
    full_issue = await common.fetch_linear_issue_details(issue_id) or issue_data

    team = full_issue.get("team") or issue_data.get("team") or {}
    project = full_issue.get("project") or {}
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
        "user_email": None,
        "source": "linear",
    }

    await common.upsert_agent_thread_owner_metadata(
        thread_id,
        source="linear",
        repo_config=repo_config,
        github_login="",
        user_email="",
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
