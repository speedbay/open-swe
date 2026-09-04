"""Tests for the gate-breach approval dashboard API (OPE-10, OPE-75)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastapi import HTTPException

from agent.speedbay import gate_approval as ga
from agent.speedbay import gate_approval_api

FP = "fp-1"
OWNER_SESSION = {"sub": "owner", "email": "owner@example.com"}


class FakeThreads:
    def __init__(self, metadata: dict[str, dict[str, Any]]) -> None:
        self.metadata = metadata
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def get(self, thread_id: str) -> dict[str, Any]:
        return {"metadata": dict(self.metadata.get(thread_id, {}))}

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.metadata.setdefault(thread_id, {}).update(metadata)
        self.updates.append((thread_id, metadata))


class FakeLangGraphClient:
    def __init__(self, threads: FakeThreads) -> None:
        self.threads = threads


def _wire_real_store(monkeypatch, threads: FakeThreads) -> None:
    client = FakeLangGraphClient(threads)
    monkeypatch.setattr(ga, "get_client", lambda: cast(Any, client))
    monkeypatch.setattr(gate_approval_api, "decide_gate_approval", ga.decide_gate_approval)
    monkeypatch.setattr(
        gate_approval_api, "restore_gate_approval_pending", ga.restore_gate_approval_pending
    )


def _pending_record(**overrides: object) -> dict:
    record = {
        "fingerprint": FP,
        "status": "pending",
        "issue_id": "OPE-10",
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "failed_rule_ids": ["atomicity"],
        "diff_stats": {"raw_loc": 400, "effective_loc": 400.0, "production_files": 1},
        "evidence_tail": "400\t0\tagent/api.py",
        "approval_url": "https://dash/agents/t-1?gateApproval=fp-1",
        "requested_at": "2026-08-01T00:00:00+00:00",
        "notified": True,
    }
    record.update(overrides)
    return record


def _wire_thread(monkeypatch, thread_id: str = "t-1", owner: str = "owner") -> None:
    async def fake_thread_metadata(tid: str) -> dict:
        assert tid == thread_id
        return {"source": "linear", "github_login": owner}

    monkeypatch.setattr(gate_approval_api, "_thread_metadata", fake_thread_metadata)


async def test_list_gate_approvals_requires_thread_owner(monkeypatch) -> None:
    _wire_thread(monkeypatch)

    async def fail_get(thread_id: str) -> dict:
        raise AssertionError("records must not be fetched for non-owners")

    monkeypatch.setattr(gate_approval_api, "get_gate_approvals", fail_get)
    with pytest.raises(HTTPException) as exc_info:
        await gate_approval_api.list_gate_approvals(
            "t-1", session={"sub": "other", "email": "other@example.com"}
        )
    assert exc_info.value.status_code == 403


async def test_list_gate_approvals_returns_records_for_owner(monkeypatch) -> None:
    _wire_thread(monkeypatch)

    async def fake_get(thread_id: str) -> dict:
        return {FP: _pending_record()}

    monkeypatch.setattr(gate_approval_api, "get_gate_approvals", fake_get)
    response = await gate_approval_api.list_gate_approvals("t-1", session=OWNER_SESSION)
    assert response["threadId"] == "t-1"
    assert response["isOwner"] is True
    (approval,) = response["approvals"]
    assert approval["fingerprint"] == FP
    assert approval["status"] == "pending"
    assert approval["issueId"] == "OPE-10"
    assert approval["failedRuleIds"] == ["atomicity"]
    assert "rounds" not in approval  # OPE-75: no corrective-round presentation
    assert approval["diffStats"]["rawLoc"] == 400


async def test_approve_decides_and_dispatches_followup(monkeypatch) -> None:
    _wire_thread(monkeypatch)
    dispatched: list[dict] = []

    async def fake_decide(thread_id: str, fingerprint: str, *, approved: bool, actor: str):
        assert (thread_id, fingerprint, approved, actor) == ("t-1", FP, True, "owner")
        return _pending_record(status="approved")

    async def fake_dispatch(thread_id: str, metadata: dict, text: str, *, plan_mode: bool):
        dispatched.append({"thread_id": thread_id, "text": text, "plan_mode": plan_mode})

    monkeypatch.setattr(gate_approval_api, "decide_gate_approval", fake_decide)
    monkeypatch.setattr(gate_approval_api, "_dispatch_followup", fake_dispatch)

    response = await gate_approval_api.approve_gate_breach("t-1", FP, session=OWNER_SESSION)
    assert response == {"status": "approved", "fingerprint": FP}
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == "t-1"
    assert dispatched[0]["plan_mode"] is False
    assert "open_pull_request" in dispatched[0]["text"]
    assert "do not amend commits" in dispatched[0]["text"]  # same diff, no alteration


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (RuntimeError("dispatch failed"), "dispatch failed"),
        (asyncio.CancelledError("dispatch cancelled"), "dispatch cancelled"),
    ],
)
async def test_approve_dispatch_failure_restores_pending_for_retry(
    monkeypatch, failure, match
) -> None:
    _wire_thread(monkeypatch)
    unrelated = _pending_record(fingerprint="unrelated", requested_at="2026-08-02T00:00:00+00:00")
    threads = FakeThreads(
        {
            "t-1": {
                ga.GATE_APPROVALS_KEY: {
                    FP: _pending_record(),
                    "unrelated": unrelated,
                }
            }
        }
    )
    _wire_real_store(monkeypatch, threads)
    dispatched: list[str] = []

    async def scripted_dispatch(
        thread_id: str, metadata: dict, text: str, *, plan_mode: bool
    ) -> None:
        dispatched.append(thread_id)
        if len(dispatched) == 1:
            raise failure

    monkeypatch.setattr(gate_approval_api, "_dispatch_followup", scripted_dispatch)

    with pytest.raises(type(failure), match=match):
        await gate_approval_api.approve_gate_breach("t-1", FP, session=OWNER_SESSION)

    approvals = threads.metadata["t-1"][ga.GATE_APPROVALS_KEY]
    assert approvals[FP]["status"] == ga.GATE_APPROVAL_PENDING
    assert "decided_at" not in approvals[FP]
    assert "decided_by" not in approvals[FP]
    assert approvals["unrelated"] == unrelated

    response = await gate_approval_api.approve_gate_breach("t-1", FP, session=OWNER_SESSION)
    assert response == {"status": "approved", "fingerprint": FP}
    assert dispatched == ["t-1", "t-1"]
    assert threads.metadata["t-1"][ga.GATE_APPROVALS_KEY][FP]["status"] == ga.GATE_APPROVAL_APPROVED


async def test_approve_success_stays_approved_and_does_not_redispatch(monkeypatch) -> None:
    _wire_thread(monkeypatch)
    threads = FakeThreads({"t-1": {ga.GATE_APPROVALS_KEY: {FP: _pending_record()}}})
    _wire_real_store(monkeypatch, threads)
    dispatched: list[str] = []

    async def successful_dispatch(
        thread_id: str, metadata: dict, text: str, *, plan_mode: bool
    ) -> None:
        dispatched.append(thread_id)

    monkeypatch.setattr(gate_approval_api, "_dispatch_followup", successful_dispatch)

    assert await gate_approval_api.approve_gate_breach("t-1", FP, session=OWNER_SESSION) == {
        "status": "approved",
        "fingerprint": FP,
    }
    assert threads.metadata["t-1"][ga.GATE_APPROVALS_KEY][FP]["status"] == ga.GATE_APPROVAL_APPROVED
    with pytest.raises(HTTPException) as exc_info:
        await gate_approval_api.approve_gate_breach("t-1", FP, session=OWNER_SESSION)
    assert exc_info.value.status_code == 404
    assert dispatched == ["t-1"]


async def test_approve_unknown_fingerprint_404s(monkeypatch) -> None:
    _wire_thread(monkeypatch)

    async def fake_decide(*args, **kwargs):
        return None

    monkeypatch.setattr(gate_approval_api, "decide_gate_approval", fake_decide)
    with pytest.raises(HTTPException) as exc_info:
        await gate_approval_api.approve_gate_breach("t-1", "nope", session=OWNER_SESSION)
    assert exc_info.value.status_code == 404


async def test_reject_posts_linear_comment_and_dispatches_nothing(monkeypatch) -> None:
    _wire_thread(monkeypatch)
    comments: list[tuple[str, str]] = []

    async def fake_decide(thread_id: str, fingerprint: str, *, approved: bool, actor: str):
        assert approved is False
        return _pending_record(status="rejected")

    async def fake_comment(issue_id: str, body: str) -> bool:
        comments.append((issue_id, body))
        return True

    async def fail_dispatch(*args, **kwargs):
        raise AssertionError("reject must not dispatch a follow-up run")

    monkeypatch.setattr(gate_approval_api, "decide_gate_approval", fake_decide)
    monkeypatch.setattr(gate_approval_api, "comment_on_linear_issue", fake_comment)
    monkeypatch.setattr(gate_approval_api, "_dispatch_followup", fail_dispatch)

    response = await gate_approval_api.reject_gate_breach("t-1", FP, session=OWNER_SESSION)
    assert response == {"status": "rejected", "fingerprint": FP}
    assert len(comments) == 1
    issue_id, body = comments[0]
    assert issue_id == "OPE-10"
    assert "rejected" in body and FP in body
    assert "https://dash/agents/t-1?gateApproval=fp-1" in body
    # OPE-75: rejection directs a human to pi-forge planning rework/split.
    assert "pi-forge" in body
    assert "rework or split" in body


def test_module_binds_no_linear_state_mutation_seam() -> None:
    """OPE-75 AC 3: neither decision path may mutate Linear issue state.

    Namespace sentinel: no callable bound anywhere in the module — under any
    alias — may be a known Linear state-mutation function. Inspecting module
    bindings (``__name__`` of every bound callable) catches an aliased
    ``from ... import update_issue as x`` that a source-text scan misses,
    and cannot false-positive on comments or docstrings. Purely behavioral
    stubbing is not available here: the module imports no mutation seam, so
    there is nothing to stub — the absence of such a binding is exactly what
    this test proves. The approve/reject behavioral paths are covered by the
    endpoint tests above with their failing dispatch/comment sentinels.
    """
    forbidden = {"update_issue", "linear_update_issue", "transition_issue_state"}
    bound = {
        getattr(value, "__name__", None)
        for value in vars(gate_approval_api).values()
        if callable(value)
    }
    assert not (forbidden & bound), (
        f"Linear state-mutation seam bound in module: {forbidden & bound}"
    )


async def test_reject_requires_thread_owner(monkeypatch) -> None:
    _wire_thread(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await gate_approval_api.reject_gate_breach(
            "t-1", FP, session={"sub": "other", "email": "other@example.com"}
        )
    assert exc_info.value.status_code == 403


async def test_pending_listing_returns_every_thread(monkeypatch) -> None:
    async def fake_list_pending():
        return [
            {"thread_id": "t-1", "record": _pending_record()},
            {"thread_id": "t-2", "record": _pending_record(fingerprint="fp-2")},
        ]

    async def fake_thread_metadata(thread_id: str) -> dict:
        return {"source": "linear", "github_login": "owner"}

    monkeypatch.setattr(gate_approval_api, "list_pending_gate_approvals", fake_list_pending)
    monkeypatch.setattr(gate_approval_api, "_thread_metadata", fake_thread_metadata)
    response = await gate_approval_api.list_all_pending_gate_approvals(session=OWNER_SESSION)
    assert {row["threadId"] for row in response["approvals"]} == {"t-1", "t-2"}
    assert all(row["status"] == "pending" for row in response["approvals"])


async def test_pending_listing_hides_threads_the_user_does_not_own(monkeypatch) -> None:
    async def fake_list_pending():
        return [
            {"thread_id": "t-1", "record": _pending_record()},
            {"thread_id": "t-2", "record": _pending_record(fingerprint="fp-2")},
        ]

    async def fake_thread_metadata(thread_id: str) -> dict:
        owner = "owner" if thread_id == "t-1" else "someone-else"
        return {"source": "linear", "github_login": owner}

    monkeypatch.setattr(gate_approval_api, "list_pending_gate_approvals", fake_list_pending)
    monkeypatch.setattr(gate_approval_api, "_thread_metadata", fake_thread_metadata)
    response = await gate_approval_api.list_all_pending_gate_approvals(session=OWNER_SESSION)
    assert [row["threadId"] for row in response["approvals"]] == ["t-1"]
