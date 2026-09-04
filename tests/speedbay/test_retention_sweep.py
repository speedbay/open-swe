"""Tests for the Open SWE thread retention sweep."""

from __future__ import annotations

import configparser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from agent.speedbay import retention_api
from speedbay import retention_sweep

DEPLOY_DIR = Path(__file__).resolve().parents[2] / "speedbay" / "deploy"


class _FakeThreads:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self.items = threads
        self.search_calls: list[dict[str, Any]] = []

    async def count(self) -> int:
        return len(self.items)

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        offset = kwargs["offset"]
        return self.items[offset : offset + kwargs["limit"]]


class _FakeClient:
    def __init__(self, threads: list[dict[str, Any]]) -> None:
        self.threads = _FakeThreads(threads)


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
        ]
    )
    deleted: list[str] = []

    async def delete_thread(thread_id: str, _cutoff: datetime) -> bool:
        if thread_id == "cron-idle":
            return False
        deleted.append(thread_id)
        return True

    with caplog.at_level("INFO"):
        summary = await retention_sweep.sweep(
            client, delete_thread, now=datetime(2026, 9, 4, tzinfo=UTC), days=10
        )

    assert deleted == ["old-idle", "old-error"]
    assert summary == {
        "scanned": 8,
        "deleted": 2,
        "skipped": 6,
        "cutoff": datetime(2026, 8, 25, tzinfo=UTC),
    }
    assert client.threads.search_calls == [
        {
            "limit": 8,
            "offset": 0,
            "sort_by": "created_at",
            "sort_order": "asc",
            "select": ["thread_id", "updated_at", "status"],
        }
    ]
    assert "scanned=8 deleted=2 skipped=6 cutoff=2026-08-25" in caplog.text
    assert not any(thread_id in caplog.text for thread_id in deleted)


async def test_sweep_bounds_thread_pages_to_initial_count(monkeypatch) -> None:
    monkeypatch.setattr(retention_sweep, "PAGE_SIZE", 2)
    client = _FakeClient(
        [
            _thread("old-1", "idle", "2026-08-01T00:00:00Z"),
            _thread("old-2", "error", "2026-08-02T00:00:00Z"),
            _thread("old-3", "idle", "2026-08-03T00:00:00Z"),
        ]
    )
    deleted: list[str] = []

    async def delete_thread(thread_id: str, _cutoff: datetime) -> bool:
        deleted.append(thread_id)
        return True

    await retention_sweep.sweep(
        client, delete_thread, now=datetime(2026, 9, 4, tzinfo=UTC), days=10
    )

    assert deleted == ["old-1", "old-2", "old-3"]
    assert [call["offset"] for call in client.threads.search_calls] == [0, 2]
    assert [call["limit"] for call in client.threads.search_calls] == [2, 1]


def test_atomic_delete_rechecks_current_thread_and_cron(monkeypatch) -> None:
    stale_id, resumed_id, cron_id = uuid4(), uuid4(), uuid4()
    store = {
        "threads": [
            {
                "thread_id": stale_id,
                "status": "idle",
                "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
            },
            {
                "thread_id": resumed_id,
                "status": "busy",
                "updated_at": datetime(2026, 9, 4, tzinfo=UTC),
            },
            {
                "thread_id": cron_id,
                "status": "error",
                "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
            },
        ],
        "runs": [{"thread_id": stale_id}, {"thread_id": resumed_id}],
        "crons": [{"thread_id": cron_id}],
    }
    checkpointer = type(
        "Checkpointer",
        (),
        {
            "storage": {str(stale_id): {}, str(resumed_id): {}},
            "writes": {(str(stale_id), "", "1"): {}, (str(resumed_id), "", "2"): {}},
        },
    )()
    monkeypatch.setattr(retention_api, "GLOBAL_STORE", store)
    monkeypatch.setattr(retention_api, "Checkpointer", lambda: checkpointer)
    cutoff = datetime(2026, 8, 25, tzinfo=UTC)

    assert retention_api.delete_stale_thread(stale_id, cutoff) is True
    assert retention_api.delete_stale_thread(resumed_id, cutoff) is False
    assert retention_api.delete_stale_thread(cron_id, cutoff) is False
    assert [thread["thread_id"] for thread in store["threads"]] == [resumed_id, cron_id]
    assert store["runs"] == [{"thread_id": resumed_id}]
    assert str(stale_id) not in cast(Any, checkpointer).storage
    assert all(key[0] != str(stale_id) for key in cast(Any, checkpointer).writes)


async def test_sweep_rejects_nonpositive_explicit_retention() -> None:
    client = _FakeClient([])

    async def delete_thread(_thread_id: str, _cutoff: datetime) -> bool:
        return False

    for days in (0, -1):
        try:
            await retention_sweep.sweep(client, delete_thread, days=days)
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

    class HttpClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "http://127.0.0.1:2024"

        async def __aenter__(self) -> HttpClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def sweep(actual_client: object, _delete_thread: object) -> None:
        assert actual_client is client

    monkeypatch.setenv("LANGGRAPH_URL", "https://langgraph.example")
    monkeypatch.setattr(retention_sweep, "get_client", get_client)
    monkeypatch.setattr(retention_sweep.httpx, "AsyncClient", HttpClient)
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
