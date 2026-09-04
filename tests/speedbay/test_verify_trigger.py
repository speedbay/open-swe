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

import pytest
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
    verify_trigger._transition_records.clear()  # isolate delivery state per test
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
    assert fn is verify_trigger._process_transition_delivery
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


async def test_false_and_exception_release_current_identity_for_retry():
    issue = _fixture_issue()
    verify_trigger._transition_records.clear()
    with patch.object(
        verify_trigger,
        "process_verify_dispatch",
        new=AsyncMock(side_effect=[False, True]),
    ) as dispatch:
        assert await verify_trigger._process_transition_delivery(issue) is False
        assert await verify_trigger._process_transition_delivery(issue) is True
    assert dispatch.await_count == 2

    verify_trigger._transition_records.clear()
    error = RuntimeError("dispatch failed")
    with patch.object(
        verify_trigger,
        "process_verify_dispatch",
        new=AsyncMock(side_effect=[error, True]),
    ) as dispatch:
        with pytest.raises(RuntimeError, match="dispatch failed") as raised:
            await verify_trigger._process_transition_delivery(issue)
        assert raised.value is error
        assert await verify_trigger._process_transition_delivery(issue) is True
    assert dispatch.await_count == 2


async def test_concurrent_duplicate_dispatches_once():
    issue = _fixture_issue()
    verify_trigger._transition_records.clear()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_dispatch(_: dict[str, Any]) -> bool:
        entered.set()
        await release.wait()
        return True

    with patch.object(
        verify_trigger, "process_verify_dispatch", side_effect=held_dispatch
    ) as dispatch:
        first = asyncio.create_task(verify_trigger._process_transition_delivery(issue))
        await entered.wait()
        second = asyncio.create_task(verify_trigger._process_transition_delivery(issue))
        assert await second is False
        assert dispatch.await_count == 1
        release.set()
        assert await first is True


async def test_full_pending_transition_map_dispatches_untracked_delivery():
    issue = _fixture_issue()
    watermark = verify_trigger._parse_updated_at(issue["updatedAt"])
    assert watermark is not None
    verify_trigger._transition_records.clear()
    for index in range(verify_trigger._SEEN_MAX):
        issue_id = f"pending-{index}"
        verify_trigger._transition_records[issue_id] = verify_trigger._TransitionRecord(
            (issue_id, issue["updatedAt"]), watermark, "pending"
        )

    with patch.object(
        verify_trigger, "process_verify_dispatch", new=AsyncMock(return_value=True)
    ) as dispatch:
        assert await verify_trigger._process_transition_delivery(issue) is True
        dispatch.assert_awaited_once_with(issue)


async def test_cancelled_dispatch_releases_current_identity_for_retry():
    issue = _fixture_issue()
    verify_trigger._transition_records.clear()
    cancelled = asyncio.CancelledError()
    with patch.object(
        verify_trigger,
        "process_verify_dispatch",
        new=AsyncMock(side_effect=[cancelled, True]),
    ) as dispatch:
        with pytest.raises(asyncio.CancelledError) as raised:
            await verify_trigger._process_transition_delivery(issue)
        assert raised.value is cancelled
        assert await verify_trigger._process_transition_delivery(issue) is True
    assert dispatch.await_count == 2


async def test_newer_transition_suppresses_delayed_older_transition():
    older = _fixture_issue()
    newer = _fixture_issue()
    newer["updatedAt"] = "2026-07-31T00:00:00.000Z"
    verify_trigger._transition_records.clear()
    dispatched: list[str] = []
    older_started = asyncio.Event()
    release_older = asyncio.Event()

    async def fake_dispatch(issue: dict[str, Any]) -> bool:
        dispatched.append(issue["updatedAt"])
        if issue is older:
            older_started.set()
            await release_older.wait()
        return True

    with patch.object(verify_trigger, "process_verify_dispatch", side_effect=fake_dispatch):
        older_task = asyncio.create_task(verify_trigger._process_transition_delivery(older))
        await older_started.wait()
        assert await verify_trigger._process_transition_delivery(newer) is True
        release_older.set()
        assert await older_task is True
        assert await verify_trigger._process_transition_delivery(older) is False

    assert dispatched == [older["updatedAt"], newer["updatedAt"]]
    assert verify_trigger._transition_records[
        older["id"]
    ].watermark == verify_trigger._parse_updated_at(newer["updatedAt"])


async def test_newer_reentry_dispatches_once():
    older = _fixture_issue()
    newer = _fixture_issue()
    newer["updatedAt"] = "2026-07-31T00:00:00.000Z"
    verify_trigger._transition_records.clear()
    dispatched: list[str] = []

    async def fake_dispatch(issue: dict[str, Any]) -> bool:
        dispatched.append(issue["updatedAt"])
        return True

    with patch.object(verify_trigger, "process_verify_dispatch", side_effect=fake_dispatch):
        assert await verify_trigger._process_transition_delivery(older) is True
        assert await verify_trigger._process_transition_delivery(newer) is True
        assert await verify_trigger._process_transition_delivery(newer) is False

    assert dispatched == [older["updatedAt"], newer["updatedAt"]]


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
    assignee_email: str | None = None,
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
        captured["upsert_kwargs"] = kwargs

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
        patch.object(
            verify_trigger,
            "_assignee_email",
            new_callable=AsyncMock,
            return_value=assignee_email,
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
    assert "The only requirements\n   that bear on the verdict" in prompt
    assert "it never creates an additional criterion" in prompt
    assert (
        "target repository and working\n   directory, platform, setup/environment, and expected result"
        in prompt
    )
    assert "acceptance criterion remains mandatory" in prompt
    assert "mark that criterion missing and name the absent declaration" in prompt
    assert "observed\n   versus expected result" in prompt
    assert "an expected\n   nonzero exit that occurs as declared satisfies the criterion" in prompt
    assert "GitHub Actions declarations use PR-head evidence." in prompt
    assert (
        "matching check at the\n     PR head SHA and record its SHA, status, and conclusion"
        in prompt
    )
    assert "Routed-repository sandbox declarations use the merge SHA." in prompt
    assert "supporting command omits context or targets another repository" in prompt
    assert "without changing an otherwise-supported verdict" in prompt
    assert "OPE-94: PR #66's historical warehouse `npm run render:check`" in prompt
    assert "do not run it from the routed open-swe root" in prompt
    assert "OPE-88: PR #909 declared GitHub Actions CI the arbiter" in prompt
    assert "do not rerun the command\n     as root on another platform" in prompt
    # OPE-52: the verifier writes checkbox results back to the issue body —
    # satisfied criteria `[ ]` → `[x]`, body otherwise byte-identical, and the
    # verdict is posted even if the description edit fails.
    assert "satisfied criterion's `[ ]`\nto `[x]`" in prompt
    assert "byte-identical" in prompt
    assert "still post the verdict" in prompt
    # OPE-52: ops-shaped criteria stay `incomplete` and the comment names the
    # drafting-defect extraction guidance instead of a generic "missing".
    assert "ops-shaped criterion — convert to code or extract" in prompt
    assert "to a linked HITL ops issue per the planning contract." in prompt
    assert "`done` = `state-done`" in prompt
    assert "`incomplete` = `state-inc`" in prompt
    assert "Please analyze this issue and implement" not in prompt
    assert captured["configurable"]["repo"] == {"owner": "speedbay", "name": "warehouse"}
    assert captured["configurable"]["linear_issue"]["identifier"] == "OPE-41"
    assert captured["upsert_kwargs"]["title"] == "Verify: OPE-41"
    assert captured["trace_called"] == 0  # single-comment write-back: no "On it!"


def test_unassigned_issue_dispatches_with_empty_attribution():
    # OPE-48 AC2: no assignee email -> the owner upsert carries empty
    # attribution (today's behavior) and the run still dispatches.
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
        assignee_email=None,
    )
    assert captured["dispatched"] is True
    assert captured["upsert_kwargs"]["user_email"] == ""
    assert captured["upsert_kwargs"]["github_login"] == ""


def test_assignee_attributed_on_thread_upsert_only():
    # OPE-48 AC3: the thread upsert carries the assignee email (login stays
    # empty — the upsert resolves it internally) while run auth is unchanged:
    # configurable["user_email"] must remain None so the run keeps the bot
    # identity instead of the assignee's GitHub OAuth token.
    issue = _fixture_issue()
    captured = _run_dispatch(
        issue,
        full_issue=issue,
        team_repo={"owner": "speedbay", "name": "warehouse"},
        assignee_email="Owner@speedbay.com",
    )
    assert captured["dispatched"] is True
    assert captured["upsert_kwargs"]["user_email"] == "Owner@speedbay.com"
    assert captured["upsert_kwargs"]["github_login"] == ""
    assert captured["configurable"]["user_email"] is None


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
