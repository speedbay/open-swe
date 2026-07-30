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
``dispatch_agent_run`` routes through ``create_durable_run`` with a
deterministic per-issue thread id and ``if_not_exists="create"`` plus
``multitask_strategy="interrupt"`` — duplicates collapse into the same
thread. A later re-entry into ready-for-verify (incomplete → rework → merge)
resumes that same thread with the prior verification context.

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


def _is_foreign_issue_event(payload: dict[str, Any]) -> bool:
    """OPE-36 trigger-owner scoping for Issue events.

    Unscoped instances accept every transition. Scoped instances act only when
    the transition's ``actor.email`` matches an owner email, and fail closed
    when the payload carries no actor email (e.g. a bot-actor transition) —
    the same trade OPE-36 makes for comments: better an unverified issue a
    human notices than two instances publishing rival verdicts.
    """
    owners = trigger_owner_emails()
    if not owners:
        return False
    email = (payload.get("actor") or {}).get("email")
    if not isinstance(email, str) or not email.strip():
        logger.info("verify trigger-owner scope: no actor email in payload — dropping")
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
    if _is_foreign_issue_event(payload):
        return {
            "status": "ignored",
            "reason": "Transition outside this instance's trigger-owner scope",
        }
    if not data.get("id"):
        return {"status": "ignored", "reason": "Issue payload has no id"}
    logger.info("Verify transition accepted for %s", identifier)
    background_tasks.add_task(process_verify_dispatch, data)
    return {"status": "accepted", "trigger": "verify-completion", "issue": identifier}


def _build_prompt(full_issue: dict[str, Any], repo_config: dict[str, str]) -> str:
    """The run prompt: issue context first, then the verification contract."""
    identifier = full_issue.get("identifier", "")
    title = full_issue.get("title", "No title")
    description = full_issue.get("description") or "No description"
    url = full_issue.get("url", "")
    issue_id = full_issue.get("id", "")
    return (
        f"Verify completion of the following Linear issue:\n\n"
        f"## Repository: {repo_config.get('owner')}/{repo_config.get('name')}\n\n"
        f"## Linear Ticket: {identifier} - Ticket ID: {issue_id}\n\n"
        f"## Linear Ticket URL: {url}\n\n"
        f"## Title: {title}\n\n"
        f"## Description:\n{description}\n\n"
        f"{_verify_contract()}"
    )


async def process_verify_dispatch(issue_data: dict[str, Any]) -> None:
    """Resolve repo + issue context and dispatch the verification run.

    Mirrors upstream's ``process_linear_issue`` dispatch shape (thread owner
    metadata, ``dispatch_agent_run``, trace comment) but with a distinct
    deterministic thread id — ``verify:<issue-id>`` — so verification never
    interrupts the issue's implementation thread, and with the verification
    contract as the prompt instead of the implementation prompt.
    """
    issue_id = issue_data.get("id", "")
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
        return
    if not common._is_repo_allowed(repo_config):
        logger.warning(
            "Verify dispatch for %s dropped: repo %s/%s not in allowlist",
            issue_id,
            repo_config.get("owner"),
            repo_config.get("name"),
        )
        return

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
    run = await common.dispatch_agent_run(
        thread_id,
        _build_prompt(full_issue, repo_config),
        configurable,
        source="linear",
        metadata=common._AGENT_VERSION_METADATA,
    )
    logger.info(
        "Verification run dispatched for %s (thread=%s run=%s)",
        identifier or issue_id,
        thread_id,
        run.get("run_id") if isinstance(run, dict) else None,
    )
    await common.post_linear_trace_comment(issue_id, thread_id, "")
