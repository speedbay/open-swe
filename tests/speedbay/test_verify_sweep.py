"""Tests for the ready-for-verify reconciliation sweep (OPE-42)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph_sdk.schema import MultitaskStrategy

from agent.speedbay import verify_sweep, verify_trigger


class _NotFound(Exception):
    status_code = 404


class _Conflict(Exception):
    status_code = 409


class _FakeThreads:
    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses

    async def get(self, thread_id: str) -> dict[str, str]:
        response = self.responses.get(thread_id, _NotFound())
        if isinstance(response, Exception):
            raise response
        return {"status": response}


class _FakeClient:
    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.threads = _FakeThreads(responses)


def _issue(n: int, *, age_hours: float = 5.0) -> dict[str, Any]:
    updated = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return {
        "id": f"issue-{n}",
        "identifier": f"OPE-{n}",
        "updatedAt": updated,
        "team": {"id": "team-1", "name": "Open SWE", "key": "OPE"},
        "stateHistory": {
            "nodes": [
                {
                    "startedAt": updated,
                    "endedAt": None,
                    "state": {"name": "ready-for-verify"},
                }
            ]
        },
    }


async def _run_sweep(
    monkeypatch: pytest.MonkeyPatch,
    issues: list[dict[str, Any]],
    *,
    busy_ids: set[str] | None = None,
    thread_error_ids: set[str] | None = None,
    fail_ids: set[str] | None = None,
    dropped_ids: set[str] | None = None,
    conflict_ids: set[str] | None = None,
    comments: dict[str, Any] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Run the public sweep with its Linear, LangGraph, and dispatch boundaries faked."""
    dispatched: list[str] = []
    failing = fail_ids or set()
    dropped = dropped_ids or set()
    conflicts = conflict_ids or set()
    thread_responses: dict[str, str | Exception] = {}
    for issue_id in busy_ids or set():
        thread_id = verify_sweep.common.generate_thread_id_from_issue(f"verify:{issue_id}")
        thread_responses[thread_id] = "busy"
    for issue_id in thread_error_ids or set():
        thread_id = verify_sweep.common.generate_thread_id_from_issue(f"verify:{issue_id}")
        thread_responses[thread_id] = RuntimeError("api down")

    async def fake_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "VerifySweepIssueComments" in query:
            result = (comments or {}).get(variables["id"], {"comments": []})
            if isinstance(result, Exception):
                raise result
            if "error" in result:
                return result
            nodes = result["comments"]
            offset = int(variables.get("after") or 0)
            next_offset = offset + 50
            return {
                "issue": {
                    "comments": {
                        "nodes": nodes[offset:next_offset],
                        "pageInfo": {
                            "hasNextPage": next_offset < len(nodes),
                            "endCursor": str(next_offset) if next_offset < len(nodes) else None,
                        },
                    }
                }
            }
        return {
            "issues": {
                "nodes": issues,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }

    async def fake_dispatch(
        issue_data: dict[str, Any], *, multitask_strategy: MultitaskStrategy = "interrupt"
    ) -> bool:
        assert multitask_strategy == "reject"
        if issue_data["id"] in failing:
            raise RuntimeError("boom")
        if issue_data["id"] in conflicts:
            raise _Conflict()
        if issue_data["id"] in dropped:
            return False
        dispatched.append(issue_data["identifier"])
        return True

    monkeypatch.setattr(verify_sweep, "_graphql_request", fake_graphql)

    async def fake_viewer_id() -> str:
        return "runtime-user"

    monkeypatch.setattr(verify_sweep.linear_guard, "_viewer_id", fake_viewer_id)
    monkeypatch.setattr(verify_sweep, "langgraph_client", lambda: _FakeClient(thread_responses))
    monkeypatch.setattr(verify_sweep.verify_trigger, "process_verify_dispatch", fake_dispatch)

    counts = await verify_sweep.sweep_stale_verify_issues(min_age_seconds=3600)
    return counts, dispatched


async def test_stale_issue_is_redispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    counts, dispatched = await _run_sweep(monkeypatch, [_issue(1)])
    assert dispatched == ["OPE-1"]
    assert counts == {"checked": 1, "skipped_busy": 0, "dispatched": 1, "errors": 0}


async def test_busy_verify_thread_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    counts, dispatched = await _run_sweep(monkeypatch, [_issue(1), _issue(2)], busy_ids={"issue-1"})
    assert dispatched == ["OPE-2"]
    assert counts["skipped_busy"] == 1
    assert counts["dispatched"] == 1


async def test_dispatch_race_is_rejected_without_interrupting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts, dispatched = await _run_sweep(monkeypatch, [_issue(1)], conflict_ids={"issue-1"})
    assert dispatched == []
    assert counts == {"checked": 1, "skipped_busy": 1, "dispatched": 0, "errors": 0}


async def test_dropped_issue_is_not_counted_as_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts, dispatched = await _run_sweep(monkeypatch, [_issue(1)], dropped_ids={"issue-1"})
    assert dispatched == []
    assert counts == {"checked": 1, "skipped_busy": 0, "dispatched": 0, "errors": 0}


async def test_one_failing_issue_does_not_abort_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts, dispatched = await _run_sweep(
        monkeypatch, [_issue(1), _issue(2), _issue(3)], fail_ids={"issue-2"}
    )
    assert dispatched == ["OPE-1", "OPE-3"]
    assert counts == {"checked": 3, "skipped_busy": 0, "dispatched": 2, "errors": 1}


async def test_fresh_issues_are_excluded_by_the_query_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_graphql(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        captured["cutoff"] = variables["cutoff"]
        return {
            "issues": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }

    monkeypatch.setattr(verify_sweep, "_graphql_request", fake_graphql)
    counts = await verify_sweep.sweep_stale_verify_issues(min_age_seconds=3600)

    assert counts["checked"] == 0
    cutoff = datetime.fromisoformat(captured["cutoff"])
    assert cutoff <= datetime.now(UTC) - timedelta(seconds=3599)


async def test_thread_inspection_error_fails_closed_as_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts, dispatched = await _run_sweep(monkeypatch, [_issue(1)], thread_error_ids={"issue-1"})
    assert dispatched == []
    assert counts["skipped_busy"] == 1


@pytest.mark.parametrize("verdict", ("done", "incomplete"))
async def test_paginated_current_cycle_terminal_verdict_is_not_redispatched_after_restart(
    monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    issue = _issue(1)
    monkeypatch.setattr(verify_trigger, "_seen_transitions", {issue["id"]: "previous-transition"})
    verify_trigger._seen_transitions.clear()
    counts, dispatched = await _run_sweep(
        monkeypatch,
        [issue],
        comments={
            issue["id"]: {
                "comments": [
                    *[
                        {
                            "createdAt": issue["stateHistory"]["nodes"][0]["startedAt"],
                            "body": "unrelated",
                            "user": {"id": "runtime-user"},
                        }
                        for _ in range(50)
                    ],
                    {
                        "createdAt": issue["stateHistory"]["nodes"][0]["startedAt"],
                        "body": f"## Completion verification\nVerdict: {verdict}",
                        "user": {"id": "runtime-user"},
                    },
                ]
            }
        },
    )
    assert dispatched == []
    assert counts == {"checked": 1, "skipped_busy": 0, "dispatched": 0, "errors": 0}


@pytest.mark.parametrize(
    ("body", "historical", "author_id"),
    (
        (None, False, "runtime-user"),
        ("unrelated", False, "runtime-user"),
        ("## Completion verification", False, "runtime-user"),
        ("Verdict: done", False, "runtime-user"),
        ("## Completion verification\nVerdict: done\nVerdict: incomplete", False, "runtime-user"),
        ("## Completion verification\nVerdict: other", False, "runtime-user"),
        ("## Completion verification\nVerdict: done", True, "runtime-user"),
        ("## Completion verification\nVerdict: done", False, "another-user"),
    ),
)
async def test_nonterminal_or_historical_comments_preserve_redispatch(
    monkeypatch: pytest.MonkeyPatch, body: str | None, historical: bool, author_id: str
) -> None:
    issue = _issue(1)
    started_at = datetime.fromisoformat(issue["stateHistory"]["nodes"][0]["startedAt"])
    comments = (
        []
        if body is None
        else [
            {
                "createdAt": (started_at - timedelta(seconds=1)).isoformat()
                if historical
                else started_at.isoformat(),
                "body": body,
                "user": {"id": author_id},
            }
        ]
    )
    counts, dispatched = await _run_sweep(
        monkeypatch, [issue], comments={issue["id"]: {"comments": comments}}
    )
    assert dispatched == ["OPE-1"]
    assert counts == {"checked": 1, "skipped_busy": 0, "dispatched": 1, "errors": 0}


async def test_comment_read_error_fails_closed_then_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = _issue(1)
    first_counts, first_dispatched = await _run_sweep(
        monkeypatch, [issue], comments={issue["id"]: {"error": "Linear unavailable"}}
    )
    second_counts, second_dispatched = await _run_sweep(monkeypatch, [issue])
    assert first_dispatched == []
    assert first_counts == {"checked": 1, "skipped_busy": 0, "dispatched": 0, "errors": 1}
    assert second_dispatched == ["OPE-1"]
    assert second_counts == {"checked": 1, "skipped_busy": 0, "dispatched": 1, "errors": 0}


async def test_scheduler_routes_verify_sweep_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import scheduler as scheduler_module

    calls: list[bool] = []

    async def fake_sweep() -> dict[str, int]:
        calls.append(True)
        return {"checked": 0, "skipped_busy": 0, "dispatched": 0, "errors": 0}

    monkeypatch.setattr(verify_sweep, "sweep_stale_verify_issues", fake_sweep)
    graph = scheduler_module.get_scheduler()
    result = await graph.ainvoke({}, config={"configurable": {"task": "verify_sweep"}})

    assert calls == [True]
    assert result["result"] == {
        "checked": 0,
        "skipped_busy": 0,
        "dispatched": 0,
        "errors": 0,
    }
