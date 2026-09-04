"""Tests for the Open SWE thread retention sweep."""

from __future__ import annotations

import configparser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from speedbay import retention_sweep

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "speedbay" / "deploy"


class _FakeThreads:
    def __init__(
        self, threads: list[dict[str, Any]], current_threads: list[dict[str, Any]] | None = None
    ) -> None:
        self.items = threads
        self.current = {thread["thread_id"]: thread for thread in current_threads or threads}
        self.search_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        offset = kwargs["offset"]
        return self.items[offset : offset + kwargs["limit"]]

    async def get(self, thread_id: str) -> dict[str, Any]:
        return self.current[thread_id]

    async def delete(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _FakeCrons:
    def __init__(
        self, crons: list[dict[str, Any]], current_crons: list[dict[str, Any]] | None = None
    ) -> None:
        self.items = crons
        self.current = current_crons or crons
        self.search_calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        if thread_id := kwargs.get("thread_id"):
            return [cron for cron in self.current if cron.get("thread_id") == thread_id][
                : kwargs["limit"]
            ]
        offset = kwargs["offset"]
        return self.items[offset : offset + kwargs["limit"]]


class _FakeClient:
    def __init__(
        self,
        threads: list[dict[str, Any]],
        crons: list[dict[str, Any]] | None = None,
        *,
        current_threads: list[dict[str, Any]] | None = None,
        current_crons: list[dict[str, Any]] | None = None,
    ) -> None:
        self.threads = _FakeThreads(threads, current_threads)
        self.crons = _FakeCrons(crons or [], current_crons)


def _thread(thread_id: str, status: str, updated_at: str) -> dict[str, str]:
    return {"thread_id": thread_id, "status": status, "updated_at": updated_at}


async def test_sweep_deletes_exactly_old_idle_and_error_threads(caplog) -> None:
    client = _FakeClient(
        [
            _thread("old-idle", "idle", "2026-08-01T00:00:00Z"),
            _thread("cron-idle", "idle", "2026-08-02T00:00:00Z"),
            _thread("old-error", "error", "2026-08-03T00:00:00Z"),
            _thread("old-busy", "busy", "2026-08-04T00:00:00Z"),
            _thread("old-interrupted", "interrupted", "2026-08-05T00:00:00Z"),
            _thread("boundary-idle", "idle", "2026-08-25T00:00:00Z"),
            _thread("new-idle", "idle", "2026-08-30T00:00:00Z"),
            _thread("new-error", "error", "2026-09-01T00:00:00Z"),
        ],
        [{"thread_id": "cron-idle"}],
    )

    with caplog.at_level("INFO"):
        summary = await retention_sweep.sweep(client, now=datetime(2026, 9, 4, tzinfo=UTC), days=10)

    assert client.threads.deleted == ["old-idle", "old-error"]
    assert summary == {
        "scanned": 8,
        "deleted": 2,
        "skipped": 6,
        "cutoff": datetime(2026, 8, 25, tzinfo=UTC),
    }
    assert client.threads.search_calls == [
        {
            "limit": 100,
            "offset": 0,
            "sort_by": "updated_at",
            "sort_order": "asc",
            "select": ["thread_id", "updated_at", "status"],
        }
    ]
    assert [call["thread_id"] for call in client.crons.search_calls[1:]] == [
        "old-idle",
        "old-error",
    ]
    assert "scanned=8 deleted=2 skipped=6 cutoff=2026-08-25" in caplog.text
    assert not any(thread_id in caplog.text for thread_id in client.threads.deleted)


async def test_sweep_pages_threads_and_crons(monkeypatch) -> None:
    monkeypatch.setattr(retention_sweep, "PAGE_SIZE", 2)
    client = _FakeClient(
        [
            _thread("old-1", "idle", "2026-08-01T00:00:00Z"),
            _thread("old-2", "error", "2026-08-02T00:00:00Z"),
            _thread("old-3", "idle", "2026-08-03T00:00:00Z"),
        ],
        [{"thread_id": "other"}, {"thread_id": "old-2"}, {"thread_id": "old-3"}],
    )

    await retention_sweep.sweep(client, now=datetime(2026, 9, 4, tzinfo=UTC), days=10)

    assert client.threads.deleted == ["old-1"]
    assert [call["offset"] for call in client.threads.search_calls] == [0, 2]
    assert [call["offset"] for call in client.crons.search_calls[:2]] == [0, 2]


async def test_sweep_revalidates_threads_and_crons_before_deletion() -> None:
    client = _FakeClient(
        [
            _thread("resumed", "idle", "2026-08-01T00:00:00Z"),
            _thread("scheduled", "idle", "2026-08-02T00:00:00Z"),
        ],
        current_threads=[
            _thread("resumed", "busy", "2026-09-04T00:00:00Z"),
            _thread("scheduled", "idle", "2026-08-02T00:00:00Z"),
        ],
        current_crons=[{"thread_id": "scheduled"}],
    )

    summary = await retention_sweep.sweep(client, now=datetime(2026, 9, 4, tzinfo=UTC), days=10)

    assert client.threads.deleted == []
    assert summary["deleted"] == 0


async def test_sweep_rejects_nonpositive_explicit_retention() -> None:
    client = _FakeClient([])

    for days in (0, -1):
        try:
            await retention_sweep.sweep(client, days=days)
        except ValueError as exc:
            assert str(exc) == "retention days must be a positive integer"
        else:
            raise AssertionError("expected a validation error")


async def test_main_uses_configured_langgraph_url(monkeypatch) -> None:
    client = object()
    urls: list[str] = []

    def get_client(*, url: str) -> object:
        urls.append(url)
        return client

    async def sweep(actual_client: object) -> None:
        assert actual_client is client

    monkeypatch.setenv("LANGGRAPH_URL", "https://langgraph.example")
    monkeypatch.setattr(retention_sweep, "get_client", get_client)
    monkeypatch.setattr(retention_sweep, "sweep", sweep)

    await retention_sweep.main()

    assert urls == ["https://langgraph.example"]


def test_retention_days_defaults_to_ten(monkeypatch) -> None:
    monkeypatch.delenv("OPENSWE_THREAD_RETENTION_DAYS", raising=False)

    assert retention_sweep.retention_days() == 10


def test_retention_days_uses_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("OPENSWE_THREAD_RETENTION_DAYS", "30")

    assert retention_sweep.retention_days() == 30


def test_retention_deploy_units_run_daily_and_catch_up() -> None:
    service = configparser.ConfigParser(interpolation=None)
    timer = configparser.ConfigParser(interpolation=None)
    service.read(DEPLOY_DIR / "openswe-thread-retention.service")
    timer.read(DEPLOY_DIR / "openswe-thread-retention.timer")

    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["User"] == "openswe"
    assert service["Service"]["EnvironmentFile"] == "-/home/openswe/open-swe/.env"
    assert service["Service"]["ExecStart"] == (
        "/home/openswe/open-swe/.venv/bin/python /home/openswe/open-swe/speedbay/retention_sweep.py"
    )
    assert service["Service"]["Restart"] == "on-failure"
    assert service["Service"]["RestartSec"] == "30"
    assert timer["Timer"]["OnCalendar"] == "daily"
    assert timer["Timer"].getboolean("Persistent") is True
    assert timer["Timer"]["Unit"] == "openswe-thread-retention.service"
