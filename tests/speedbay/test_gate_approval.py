"""Tests for the OPE-10 gate-breach approval pause.

The durable record store and the middleware's escalation path are exercised
with in-memory fakes: a dict-backed LangGraph thread-metadata client and a
recording Linear client. Behavioral contract per AC: record-before-notify,
post-once, one-time fingerprint-bound exemption, re-gate on a new commit,
reject terminality, restart durability, and fail-loud fallback listing.

Run:  .venv/bin/python -m pytest tests/speedbay/test_gate_approval.py -x -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import attrs
import pytest

from agent.speedbay import gate_approval as ga
from agent.speedbay import pr_standards as fg
from agent.speedbay.pr_standards import PRStandardsMiddleware

# --- fakes -------------------------------------------------------------------


class FakeThreads:
    """In-memory stand-in for langgraph_sdk threads (get/update/search)."""

    def __init__(self, metadata: dict[str, dict[str, Any]] | None = None) -> None:
        self.metadata = metadata if metadata is not None else {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def get(self, thread_id: str) -> dict[str, Any]:
        return {"metadata": dict(self.metadata.get(thread_id, {}))}

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.metadata.setdefault(thread_id, {}).update(metadata)
        self.updates.append((thread_id, metadata))

    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
        **_: Any,
    ) -> list[dict[str, Any]]:
        records = [
            {"thread_id": tid, "metadata": dict(md)}
            for tid, md in self.metadata.items()
            if not metadata or all(md.get(k) == v for k, v in metadata.items())
        ]
        return records[offset : offset + limit]


class FakeLangGraphClient:
    def __init__(self, threads: FakeThreads | None = None) -> None:
        self.threads = threads or FakeThreads()


class FakeLinear:
    """Recording comment_on_linear_issue double; fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.comments: list[tuple[str, str]] = []
        self.fail = fail

    async def comment(self, issue_id: str, body: str, parent_id: str | None = None) -> bool:
        if self.fail:
            raise ConnectionError("linear down")
        self.comments.append((issue_id, body))
        return True


@attrs.define(frozen=True)
class FakeResponse:
    output: str = ""
    exit_code: int | None = 0


class FakeBackend:
    """Scripted sandbox-execute stub keyed on command substrings."""

    def __init__(self, script: dict[str, FakeResponse]) -> None:
        self.script = script
        self.commands: list[str] = []

    async def aexecute(self, command: str, *, timeout: int | None = None) -> FakeResponse:
        self.commands.append(command)
        for needle, response in self.script.items():
            if needle in command:
                return response
        return FakeResponse()


def _run(coro):
    return asyncio.run(coro)


def _wire_store(monkeypatch: pytest.MonkeyPatch, client: FakeLangGraphClient) -> None:
    monkeypatch.setattr(ga, "get_client", lambda: cast(Any, client))


def _wire_linear(monkeypatch: pytest.MonkeyPatch, linear: FakeLinear) -> None:
    monkeypatch.setattr(fg, "comment_on_linear_issue", linear.comment)


def _gate_backend(
    numstat: str = "400\t0\tagent/api.py\n",
    *,
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
) -> FakeBackend:
    return FakeBackend(
        {
            "diff --numstat": FakeResponse(output=numstat),
            "rev-parse ope-10-gate-breach": FakeResponse(output=head_sha),
            "rev-parse origin/main": FakeResponse(output=base_sha),
        }
    )


def _wire_gate(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    *,
    thread_id: str = "t-1",
    issue: str | None = "OPE-10",
) -> None:
    async def fake_get_backend(tid: str):
        return backend

    configurable: dict[str, Any] = {"thread_id": thread_id, "repo": {"name": "wh"}}
    if issue is not None:
        configurable["linear_issue"] = {"identifier": issue, "id": "uuid-1"}
    monkeypatch.setattr(fg, "get_sandbox_backend", fake_get_backend)
    monkeypatch.setattr(fg, "get_config", lambda: {"configurable": configurable})


def _request() -> Any:
    args = {
        "base": "main",
        "head": "ope-10-gate-breach",
        "title": "OPE-10: add the pause",
        "body": (
            "Closes OPE-10\n\n## Why needed\nx\n\n## Solved / fixed\nx\n\n"
            "## Workflow enabled / fixed\nx\n\n## Verification\nx\n"
        ),
    }

    class Request:
        tool_call = {"name": "open_pull_request", "args": args, "id": "call-1"}

        def override(self, *, tool_call: dict[str, Any]) -> Request:
            new = Request()
            new.tool_call = tool_call
            return new

    return Request()


async def _pass_handler(request: Any) -> Any:
    return "pr-opened"


async def _fail_handler(request: Any) -> Any:  # pragma: no cover - must not run
    raise AssertionError("handler must not run when the gate blocks")


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(str(result.content))


FP = ga.gate_fingerprint("b" * 40, "h" * 40, ["atomicity"])


# --- record-before-notify, post-once (AC 1) -----------------------------------


def test_escalation_creates_record_before_linear_post_and_posts_once(monkeypatch) -> None:
    client, linear = FakeLangGraphClient(), FakeLinear()
    _wire_store(monkeypatch, client)
    _wire_linear(monkeypatch, linear)
    backend = _gate_backend()
    _wire_gate(monkeypatch, backend)

    middleware = PRStandardsMiddleware()
    events: list[str] = []

    real_comment = linear.comment

    async def ordered_comment(issue_id: str, body: str, parent_id: str | None = None) -> bool:
        # At notify time the durable record must already exist.
        record = client.threads.metadata["t-1"][ga.GATE_APPROVALS_KEY].get(FP)
        events.append("record-exists" if record else "no-record")
        return await real_comment(issue_id, body, parent_id)

    monkeypatch.setattr(fg, "comment_on_linear_issue", ordered_comment)

    for _ in range(2):
        payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
        assert payload["escalation_required"] is False
        assert not linear.comments  # nothing posts before the cap

    payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["escalation_required"] is True
    assert payload["recoverable_by_agent"] is False
    assert payload["gate_approval"]["fingerprint"] == FP
    assert payload["gate_approval"]["status"] == "pending"
    assert "gateApproval=" in payload["gate_approval"]["approval_url"]
    assert events == ["record-exists"]

    record = client.threads.metadata["t-1"][ga.GATE_APPROVALS_KEY][FP]
    assert record["status"] == "pending"
    assert record["rounds"] == 3
    assert record["issue_id"] == "uuid-1"  # the UUID, so the escalation comment lands
    assert record["base_sha"] == "b" * 40 and record["head_sha"] == "h" * 40
    assert record["failed_rule_ids"] == ["atomicity"]
    assert record["diff_stats"]["raw_loc"] == 400
    assert record["notified"] is True

    assert len(linear.comments) == 1
    issue_id, body = linear.comments[0]
    assert issue_id == "uuid-1"  # the Linear UUID, not the identifier
    assert "OPE-10" in body and "gate breach" in body
    assert record["approval_url"] in body

    # A fourth blocked attempt re-uses the pending record and never re-posts.
    _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert len(linear.comments) == 1


def test_linear_post_failure_keeps_record_and_logs_error(monkeypatch, caplog) -> None:
    client, linear = FakeLangGraphClient(), FakeLinear(fail=True)
    _wire_store(monkeypatch, client)
    _wire_linear(monkeypatch, linear)
    _wire_gate(monkeypatch, _gate_backend())

    middleware = PRStandardsMiddleware()
    for _ in range(2):
        _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    with caplog.at_level(logging.ERROR):
        payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))

    assert payload["escalation_required"] is True
    record = client.threads.metadata["t-1"][ga.GATE_APPROVALS_KEY][FP]
    assert record["status"] == "pending"
    assert record["notified"] is False
    assert "failed to post escalation comment" in caplog.text
    assert "uuid-1" in caplog.text and record["approval_url"] in caplog.text


# --- approved exemption: exactly once, re-gate on new commit (AC 2) -----------


def test_approved_fingerprint_passes_exactly_once_and_new_commit_regates(
    monkeypatch, caplog
) -> None:
    client, linear = FakeLangGraphClient(), FakeLinear()
    _wire_store(monkeypatch, client)
    _wire_linear(monkeypatch, linear)
    _wire_gate(monkeypatch, _gate_backend())

    middleware = PRStandardsMiddleware()
    for _ in range(3):  # escalate to the pause
        _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))

    _run(ga.decide_gate_approval("t-1", FP, approved=True, actor="owner"))

    # Approved: the same diff passes the gate exactly once.
    assert _run(middleware.awrap_tool_call(_request(), _pass_handler)) == "pr-opened"
    record = client.threads.metadata["t-1"][ga.GATE_APPROVALS_KEY][FP]
    assert record["status"] == ga.GATE_APPROVAL_CONSUMED

    # The same diff re-gates: the exemption was spent, so this is corrective
    # round 4 on the still-failing diff (corrective message, not an exemption).
    with caplog.at_level(logging.ERROR):
        payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["code"] == "pr_standards_failed"
    assert payload["corrective_round"] == 4

    # A new commit changes the fingerprint: no exemption covers it — the new
    # diff is a fresh corrective round, and its own escalation pauses again.
    _wire_gate(monkeypatch, _gate_backend(head_sha="c" * 40))
    payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["code"] == "pr_standards_failed"
    assert payload["corrective_round"] == 1
    assert payload["recoverable_by_agent"] is True
    for _ in range(2):
        payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["escalation_required"] is True
    assert payload["gate_approval"]["fingerprint"] != FP


def test_terminal_record_is_not_reopened(monkeypatch) -> None:
    client = FakeLangGraphClient()
    _wire_store(monkeypatch, client)
    _run(
        ga.ensure_gate_approval_pending(
            "t-1",
            fingerprint=FP,
            issue_id="OPE-10",
            base_sha="b" * 40,
            head_sha="h" * 40,
            failed_rule_ids=["atomicity"],
        )
    )
    _run(ga.decide_gate_approval("t-1", FP, approved=True, actor="owner"))
    record, created = _run(
        ga.ensure_gate_approval_pending(
            "t-1",
            fingerprint=FP,
            issue_id="OPE-10",
            base_sha="b" * 40,
            head_sha="h" * 40,
            failed_rule_ids=["atomicity"],
        )
    )
    assert created is False
    assert record["status"] == ga.GATE_APPROVAL_APPROVED


# --- reject terminality (AC 3) ------------------------------------------------


def test_rejected_fingerprint_blocks_forever(monkeypatch) -> None:
    client, linear = FakeLangGraphClient(), FakeLinear()
    _wire_store(monkeypatch, client)
    _wire_linear(monkeypatch, linear)
    _wire_gate(monkeypatch, _gate_backend())

    middleware = PRStandardsMiddleware()
    for _ in range(3):
        _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    _run(ga.decide_gate_approval("t-1", FP, approved=False, actor="owner"))

    payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["gate_approval"]["status"] == ga.GATE_APPROVAL_REJECTED
    assert payload["recoverable_by_agent"] is False
    assert "rejected" in payload["error"]
    assert _run(ga.consume_gate_approval("t-1", FP)) is False  # no exemption, ever


# --- durability and replay safety (AC 4) ---------------------------------------


def test_rounds_and_pending_state_survive_middleware_reinstantiation(monkeypatch) -> None:
    client, linear = FakeLangGraphClient(), FakeLinear()
    _wire_store(monkeypatch, client)
    _wire_linear(monkeypatch, linear)
    _wire_gate(monkeypatch, _gate_backend())

    # Two blocked rounds on one "process"…
    first = PRStandardsMiddleware()
    for expected in (1, 2):
        payload = _payload(_run(first.awrap_tool_call(_request(), _fail_handler)))
        assert payload["corrective_round"] == expected

    # …a fresh middleware instance (backend restart) continues from metadata,
    # not process memory: the next block escalates and does not re-notify.
    second = PRStandardsMiddleware()
    payload = _payload(_run(second.awrap_tool_call(_request(), _fail_handler)))
    assert payload["corrective_round"] == 3
    assert payload["escalation_required"] is True
    assert len(linear.comments) == 1


def test_approval_is_thread_and_fingerprint_bound(monkeypatch) -> None:
    client = FakeLangGraphClient()
    _wire_store(monkeypatch, client)
    _run(
        ga.ensure_gate_approval_pending(
            "t-1",
            fingerprint=FP,
            issue_id="OPE-10",
            base_sha="b" * 40,
            head_sha="h" * 40,
            failed_rule_ids=["atomicity"],
        )
    )
    _run(ga.decide_gate_approval("t-1", FP, approved=True, actor="owner"))

    assert _run(ga.consume_gate_approval("t-2", FP)) is False  # other thread
    other = ga.gate_fingerprint("b" * 40, "d" * 40, ["atomicity"])
    assert _run(ga.consume_gate_approval("t-1", other)) is False  # other fingerprint
    assert _run(ga.decide_gate_approval("t-2", FP, approved=True, actor="owner")) is None
    assert _run(ga.consume_gate_approval("t-1", FP)) is True  # the real one works


# --- cross-thread pending listing (AC 5) ---------------------------------------


def test_pending_listing_spans_threads_and_only_pending(monkeypatch) -> None:
    threads = FakeThreads(
        {
            "t-1": {"source": "linear", "github_login": "owner"},
            "t-2": {"source": "linear", "github_login": "owner"},
            "t-3": {"source": "linear", "github_login": "owner"},
        }
    )
    client = FakeLangGraphClient(threads)
    _wire_store(monkeypatch, client)
    for tid in ("t-1", "t-2", "t-3"):
        _run(
            ga.ensure_gate_approval_pending(
                tid,
                fingerprint=f"fp-{tid}",
                issue_id="OPE-10",
                base_sha="b" * 40,
                head_sha="h" * 40,
                failed_rule_ids=["atomicity"],
            )
        )
    _run(ga.decide_gate_approval("t-2", "fp-t-2", approved=False, actor="owner"))

    pending = _run(ga.list_pending_gate_approvals())
    assert {row["thread_id"] for row in pending} == {"t-1", "t-3"}
    assert all(row["record"]["status"] == "pending" for row in pending)


def test_failed_linear_post_still_lists_pending(monkeypatch) -> None:
    """AC 5 seam: notify fails, the record is pending, the listing surfaces it."""
    client, linear = FakeLangGraphClient(), FakeLinear(fail=True)
    _wire_store(monkeypatch, client)
    _wire_linear(monkeypatch, linear)
    _wire_gate(monkeypatch, _gate_backend())

    middleware = PRStandardsMiddleware()
    for _ in range(3):
        _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))

    pending = _run(ga.list_pending_gate_approvals())
    assert [row["thread_id"] for row in pending] == ["t-1"]
    assert pending[0]["record"]["fingerprint"] == FP
    assert pending[0]["record"]["notified"] is False


# --- store unit behavior --------------------------------------------------------


def test_mark_notified_sets_flag_and_timestamp(monkeypatch) -> None:
    client = FakeLangGraphClient()
    _wire_store(monkeypatch, client)
    _run(
        ga.ensure_gate_approval_pending(
            "t-1",
            fingerprint=FP,
            issue_id=None,
            base_sha="b" * 40,
            head_sha="h" * 40,
            failed_rule_ids=["title-format"],
        )
    )
    _run(ga.mark_gate_approval_notified("t-1", FP))
    record = _run(ga.get_gate_approvals("t-1"))[FP]
    assert record["notified"] is True
    assert isinstance(record["notified_at"], str)
    _run(ga.mark_gate_approval_notified("t-1", "missing"))  # no-op, no error


def test_bump_rounds_works_without_a_pending_record(monkeypatch) -> None:
    client = FakeLangGraphClient()
    _wire_store(monkeypatch, client)
    assert _run(ga.bump_gate_rounds("t-1", FP)) == 1
    assert _run(ga.bump_gate_rounds("t-1", FP)) == 2
    assert "status" not in _run(ga.get_gate_approvals("t-1"))[FP]
