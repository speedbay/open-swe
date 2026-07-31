"""Tests for the ready-for-verify completion-verification trigger (OPE-39).

The positive fixture ``linear_issue_update_payload.json`` is a real Linear
delivery captured live from the production webhook (OPE-38 step 4 → OPE-39
payload pinning), not a hand-written approximation. Negative cases are derived
from it by mutating exactly the field under test, so any drift between the
pinned shape and the filter is caught here.
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
from langgraph_sdk.schema import MultitaskStrategy

from agent.speedbay import verify_trigger

FIXTURE = pathlib.Path(__file__).parent / "linear_issue_update_payload.json"
COMMENT_FIXTURE = pathlib.Path(__file__).parent / "linear_comment_payload.json"


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


class _FakeBackgroundTasks(BackgroundTasks):
    """Captures add_task calls without running them."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def add_task(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append((fn, args))


def _maybe_handle(payload: dict[str, Any], *, self_authored: bool = False):
    verify_trigger._seen_transitions.clear()  # isolate the dedup guard per test
    tasks = _FakeBackgroundTasks()
    with patch.object(
        verify_trigger.linear_guard,
        "is_self_comment",
        new_callable=AsyncMock,
        return_value=self_authored,
    ):
        result = asyncio.run(verify_trigger.maybe_handle(payload, tasks))
    return result, tasks


# --- filter: is_verify_transition -------------------------------------------


def test_accepts_the_pinned_live_payload():
    assert verify_trigger.is_verify_transition(_payload())


def test_rejects_comment_events():
    comment = json.loads(COMMENT_FIXTURE.read_text())
    assert not verify_trigger.is_verify_transition(comment)
    result, tasks = _maybe_handle(comment)
    assert result is None  # falls through to upstream comment handling
    assert tasks.calls == []


def test_rejects_non_update_actions():
    p = _payload()
    p["action"] = "create"
    assert not verify_trigger.is_verify_transition(p)


def test_rejects_updates_without_a_state_change():
    p = _payload()
    del p["updatedFrom"]["stateId"]  # e.g. a description edit while parked
    assert not verify_trigger.is_verify_transition(p)


def test_rejects_transitions_to_any_other_state():
    p = _payload()
    p["data"]["state"]["name"] = "done"  # e.g. the verify run's own verdict
    assert not verify_trigger.is_verify_transition(p)


# --- maybe_handle: guards and dispatch ---------------------------------------


def test_positive_payload_queues_exactly_one_dispatch():
    result, tasks = _maybe_handle(_payload())
    assert result == {
        "status": "accepted",
        "trigger": "verify-completion",
        "issue": "OPE-41",
    }
    assert len(tasks.calls) == 1
    fn, args = tasks.calls[0]
    assert fn is verify_trigger.process_verify_dispatch
    assert args[0]["id"] == "03525271-e0dd-4de8-9871-e4cd8424a5c7"


def test_self_authored_transition_is_dropped():
    result, tasks = _maybe_handle(_payload(), self_authored=True)
    assert result is not None and result["status"] == "ignored"
    assert tasks.calls == []


def _scope_check(assignee_email: str | None) -> bool:
    with patch.object(
        verify_trigger,
        "_assignee_email",
        new_callable=AsyncMock,
        return_value=assignee_email,
    ):
        return asyncio.run(verify_trigger._is_foreign_issue("issue-1"))


def test_unscoped_instance_accepts_without_assignee_lookup(monkeypatch):
    monkeypatch.delenv("OPENSWE_TRIGGER_OWNER_EMAILS", raising=False)
    with patch.object(verify_trigger, "_assignee_email", new_callable=AsyncMock) as lookup:
        assert asyncio.run(verify_trigger._is_foreign_issue("issue-1")) is False
        assert lookup.await_count == 0  # single-instance path never hits Linear


def test_scoped_instance_drops_foreign_assignee(monkeypatch):
    monkeypatch.setenv("OPENSWE_TRIGGER_OWNER_EMAILS", "someone-else@speedbay.com")
    assert _scope_check("owner@speedbay.com") is True


def test_scoped_instance_accepts_owner_assignee(monkeypatch):
    # Scoping keys on the issue assignee, not the transition actor: the
    # ready-for-verify transition is authored by the shared merge automation,
    # which can never identify the owning operator.
    monkeypatch.setenv("OPENSWE_TRIGGER_OWNER_EMAILS", "Owner@speedbay.com")
    assert _scope_check("owner@speedbay.com") is False


def test_scoped_instance_fails_closed_when_unassigned(monkeypatch):
    monkeypatch.setenv("OPENSWE_TRIGGER_OWNER_EMAILS", "owner@speedbay.com")
    assert _scope_check(None) is True


def test_duplicate_delivery_dispatches_exactly_once():
    # Live-verified: each event arrives once per covering webhook with an
    # identical data.updatedAt; the second delivery must not start a run.
    verify_trigger._seen_transitions.clear()
    tasks = _FakeBackgroundTasks()
    with patch.object(
        verify_trigger.linear_guard,
        "is_self_comment",
        new_callable=AsyncMock,
        return_value=False,
    ):
        first = asyncio.run(verify_trigger.maybe_handle(_payload(), tasks))
        second = asyncio.run(verify_trigger.maybe_handle(_payload(), tasks))
    assert first is not None and first["status"] == "accepted"
    assert second is not None and second["status"] == "ignored"
    assert "Duplicate" in second["reason"]
    assert len(tasks.calls) == 1


def test_reentry_with_new_updated_at_dispatches_again():
    # incomplete -> rework -> merge produces a fresh transition (new updatedAt).
    verify_trigger._seen_transitions.clear()
    tasks = _FakeBackgroundTasks()
    later = _payload()
    later["data"]["updatedAt"] = "2026-07-31T00:00:00.000Z"
    with patch.object(
        verify_trigger.linear_guard,
        "is_self_comment",
        new_callable=AsyncMock,
        return_value=False,
    ):
        first = asyncio.run(verify_trigger.maybe_handle(_payload(), tasks))
        second = asyncio.run(verify_trigger.maybe_handle(later, tasks))
    assert first is not None and first["status"] == "accepted"
    assert second is not None and second["status"] == "accepted"
    assert len(tasks.calls) == 2


def test_verify_thread_is_deterministic_and_distinct_from_impl_thread():
    issue_id = _payload()["data"]["id"]
    from agent.webhooks import common

    a = common.generate_thread_id_from_issue(f"verify:{issue_id}")
    b = common.generate_thread_id_from_issue(f"verify:{issue_id}")
    assert a == b
    assert a != common.generate_thread_id_from_issue(issue_id)  # not the impl thread


# --- process_verify_dispatch --------------------------------------------------


def _run_dispatch(
    issue_data: dict[str, Any],
    *,
    full_issue: dict[str, Any] | None,
    team_repo: dict[str, str] | None = None,
    default_repo: dict[str, str] | None = None,
    allowed: bool = True,
    verdict_states: dict[str, str] | None = None,
    multitask_strategy: MultitaskStrategy = "interrupt",
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_dispatch(
        thread_id, content, configurable, *, source, metadata=None, multitask_strategy="interrupt"
    ):
        captured["thread_id"] = thread_id
        captured["content"] = content
        captured["configurable"] = configurable
        captured["source"] = source
        captured["multitask_strategy"] = multitask_strategy
        return {"run_id": "run-1"}

    async def fake_upsert(thread_id, **kwargs):
        captured["upsert_title"] = kwargs.get("title")

    with (
        patch.object(
            verify_trigger.common,
            "fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=full_issue,
        ),
        patch.object(
            verify_trigger.common,
            "get_repo_config_from_team_mapping",
            return_value=team_repo,
        ),
        patch.object(
            verify_trigger.common,
            "get_team_default_repo",
            new_callable=AsyncMock,
            return_value=default_repo,
        ),
        patch.object(verify_trigger.common, "_is_repo_allowed", return_value=allowed),
        patch.object(
            verify_trigger,
            "_verdict_state_ids",
            new_callable=AsyncMock,
            return_value=verdict_states or {},
        ),
        patch.object(verify_trigger.common, "dispatch_agent_run", side_effect=fake_dispatch),
        patch.object(
            verify_trigger.common,
            "upsert_agent_thread_owner_metadata",
            side_effect=fake_upsert,
        ),
        patch.object(
            verify_trigger.common,
            "post_linear_trace_comment",
            new_callable=AsyncMock,
        ) as trace,
    ):
        captured["dispatched"] = asyncio.run(
            verify_trigger.process_verify_dispatch(
                issue_data, multitask_strategy=multitask_strategy
            )
        )
        # The write-back contract is one Linear comment (the verdict); the
        # "On it!" trace comment must never be posted by verify dispatch.
        captured["trace_called"] = trace.await_count
    return captured


def _fixture_issue() -> dict[str, Any]:
    return copy.deepcopy(_payload()["data"])


def test_dispatch_builds_verify_prompt_on_distinct_thread():
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
        verdict_states={"done": "state-done", "incomplete": "state-inc"},
    )
    from agent.webhooks import common

    assert captured["thread_id"] == common.generate_thread_id_from_issue(f"verify:{issue['id']}")
    assert captured["dispatched"] is True
    prompt = captured["content"]
    # Issue context + the ported verification contract, not the implementation prompt.
    assert "OPE-41" in prompt
    assert "Verify completion of the following Linear issue" in prompt
    assert "A merge never means done." in prompt
    assert "Exactly one verdict: `done` or `incomplete`." in prompt
    # OPE-44: deferral is forbidden — evidence missing now means incomplete now.
    assert "Never defer. Evidence missing now means `incomplete` now." in prompt
    assert "never call `schedule_thread_wakeup`" in prompt
    assert "gh pr diff" in prompt
    assert "`done` = `state-done`" in prompt
    assert "`incomplete` = `state-inc`" in prompt
    assert "Please analyze this issue and implement" not in prompt
    assert captured["configurable"]["repo"] == {"owner": "speedbay", "name": "warehouse"}
    assert captured["configurable"]["linear_issue"]["identifier"] == "OPE-41"
    assert captured["upsert_title"] == "Verify: OPE-41"
    assert captured["trace_called"] == 0  # single-comment write-back: no "On it!"


def test_dispatch_forwards_noninterrupting_strategy():
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
        multitask_strategy="reject",
    )
    assert captured["multitask_strategy"] == "reject"


def test_dispatch_marks_unresolved_state_ids_in_prompt():
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
        verdict_states={},
    )
    assert "could not be resolved server-side" in captured["content"]


def test_dispatch_honors_body_declared_repo():
    # OPE-49: an explicit repo:owner/name in the issue body beats the team
    # mapping — verify dispatches carry no comment, so the per-comment
    # override can never apply to them.
    issue = _fixture_issue()
    full_issue = copy.deepcopy(issue)
    full_issue["description"] = "Verify against repo:speedbay/open-swe please"
    captured = _run_dispatch(
        issue,
        full_issue=full_issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
    )
    assert captured["dispatched"] is True
    assert captured["configurable"]["repo"] == {"owner": "speedbay", "name": "open-swe"}
    assert "## Repository: speedbay/open-swe" in captured["content"]


def test_dispatch_body_without_declaration_preserves_team_mapping():
    # No repo: declaration in the body routes exactly as before.
    issue = _fixture_issue()
    full_issue = copy.deepcopy(issue)
    full_issue["description"] = (
        "An ordinary description linking https://github.com/upstream/dependency/issues/1."
    )
    captured = _run_dispatch(
        issue,
        full_issue=full_issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
    )
    assert captured["configurable"]["repo"] == {"owner": "speedbay", "name": "warehouse"}


def test_dispatch_drops_disallowed_body_declared_repo():
    # The allowlist still gates body-declared repos.
    issue = _fixture_issue()
    full_issue = copy.deepcopy(issue)
    full_issue["description"] = "Verify against repo:evil/repo"
    captured = _run_dispatch(
        issue,
        full_issue=full_issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
        allowed=False,
    )
    assert "thread_id" not in captured  # no run dispatched
    assert captured["dispatched"] is False


def test_dispatch_falls_back_to_team_default_repo():
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo=None,
        default_repo={"owner": "speedbay", "name": "warehouse"},
    )
    assert captured["configurable"]["repo"] == {"owner": "speedbay", "name": "warehouse"}


def test_dispatch_drops_disallowed_repo():
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo={"owner": "evil", "name": "repo"},
        allowed=False,
    )
    assert "thread_id" not in captured  # no run dispatched
    assert captured["dispatched"] is False


def test_dispatch_survives_issue_fetch_failure():
    # fetch_linear_issue_details returning None falls back to webhook data.
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue, full_issue=None, team_repo={"owner": "speedbay", "name": "warehouse"}
    )
    assert "OPE-41" in captured["content"]
