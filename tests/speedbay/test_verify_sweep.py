"""Tests for the ready-for-verify reconciliation sweep (OPE-42)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from agent.speedbay import verify_sweep


def _issue(n: int, *, age_hours: float = 5.0) -> dict[str, Any]:
    updated = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return {
        "id": f"issue-{n}",
        "identifier": f"OPE-{n}",
        "updatedAt": updated,
        "team": {"id": "team-1", "name": "Open SWE", "key": "OPE"},
    }


def _run_sweep(
    issues: list[dict[str, Any]],
    *,
    busy_ids: set[str] | None = None,
    fail_ids: set[str] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Run the sweep with the Linear query, busy-check, and dispatch faked."""
    dispatched: list[str] = []
    busy = busy_ids or set()
    failing = fail_ids or set()

    async def fake_busy(issue_id: str) -> bool:
        return issue_id in busy

    async def fake_dispatch(issue_data: dict[str, Any]) -> None:
        if issue_data["id"] in failing:
            raise RuntimeError("boom")
        dispatched.append(issue_data["identifier"])

    with (
        patch.object(
            verify_sweep,
            "_stale_verify_issues",
            new_callable=AsyncMock,
            return_value=issues,
        ),
        patch.object(verify_sweep, "_verify_thread_busy", side_effect=fake_busy),
        patch.object(
            verify_sweep.verify_trigger, "process_verify_dispatch", side_effect=fake_dispatch
        ),
    ):
        counts = asyncio.run(verify_sweep.sweep_stale_verify_issues(min_age_seconds=3600))
    return counts, dispatched


def test_stale_issue_is_redispatched():
    counts, dispatched = _run_sweep([_issue(1)])
    assert dispatched == ["OPE-1"]
    assert counts == {"checked": 1, "skipped_busy": 0, "dispatched": 1, "errors": 0}


def test_busy_verify_thread_is_skipped():
    counts, dispatched = _run_sweep([_issue(1), _issue(2)], busy_ids={"issue-1"})
    assert dispatched == ["OPE-2"]
    assert counts["skipped_busy"] == 1
    assert counts["dispatched"] == 1


def test_one_failing_issue_does_not_abort_the_sweep():
    counts, dispatched = _run_sweep([_issue(1), _issue(2), _issue(3)], fail_ids={"issue-2"})
    assert dispatched == ["OPE-1", "OPE-3"]
    assert counts == {"checked": 3, "skipped_busy": 0, "dispatched": 2, "errors": 1}


def test_fresh_issues_are_excluded_by_the_query_cutoff():
    # Freshness filtering happens in the Linear query itself: the cutoff
    # passed to _stale_verify_issues must be at least min_age_seconds old.
    captured: dict[str, str] = {}

    async def fake_stale(cutoff_iso: str) -> list[dict[str, Any]]:
        captured["cutoff"] = cutoff_iso
        return []

    with patch.object(verify_sweep, "_stale_verify_issues", side_effect=fake_stale):
        counts = asyncio.run(verify_sweep.sweep_stale_verify_issues(min_age_seconds=3600))
    assert counts["checked"] == 0
    cutoff = datetime.fromisoformat(captured["cutoff"])
    assert cutoff <= datetime.now(UTC) - timedelta(seconds=3599)


def test_missing_verify_thread_is_not_busy():
    class _NotFound(Exception):
        status_code = 404

    class _FakeThreads:
        async def get(self, thread_id: str):
            raise _NotFound()

    class _FakeClient:
        threads = _FakeThreads()

    with patch.object(verify_sweep, "langgraph_client", return_value=_FakeClient()):
        assert asyncio.run(verify_sweep._verify_thread_busy("issue-1")) is False


def test_thread_inspection_error_fails_closed_as_busy():
    class _FakeThreads:
        async def get(self, thread_id: str):
            raise RuntimeError("api down")

    class _FakeClient:
        threads = _FakeThreads()

    with patch.object(verify_sweep, "langgraph_client", return_value=_FakeClient()):
        assert asyncio.run(verify_sweep._verify_thread_busy("issue-1")) is True


def test_scheduler_routes_verify_sweep_task():
    from agent import scheduler as scheduler_module

    with patch.object(
        verify_sweep,
        "sweep_stale_verify_issues",
        new_callable=AsyncMock,
        return_value={"checked": 0, "skipped_busy": 0, "dispatched": 0, "errors": 0},
    ) as sweep:
        graph = scheduler_module.get_scheduler()
        result = asyncio.run(graph.ainvoke({}, config={"configurable": {"task": "verify_sweep"}}))
    assert sweep.await_count == 1
    assert result["result"] == {"checked": 0, "skipped_busy": 0, "dispatched": 0, "errors": 0}
