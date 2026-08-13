"""Tests for boot-time verify-sweep cron provisioning (OPE-53)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.speedbay import verify_sweep_cron
from speedbay import ensure_verify_sweep_cron


class _FakeCrons:
    def __init__(
        self,
        existing: list[dict[str, Any]] | None = None,
        fail: bool = False,
        block_create: bool = False,
    ) -> None:
        self.existing = existing if existing is not None else []
        self.fail = fail
        self.block_create = block_create
        self.search_calls: list[dict[str, Any]] = []
        self.create_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.create_started = asyncio.Event()
        self.release_create = asyncio.Event()

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("store unavailable")
        self.search_calls.append(kwargs)
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 10)
        return self.existing[offset : offset + limit]

    async def create(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("store unavailable")
        self.create_calls.append((args, kwargs))
        if self.block_create:
            self.create_started.set()
            await self.release_create.wait()
        return {"cron_id": "new-cron-id"}


class _FakeThreads:
    def __init__(self) -> None:
        self.thread: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        if self.thread is None:
            self.thread = {"thread_id": kwargs["thread_id"], "metadata": kwargs["metadata"]}
        return self.thread

    async def delete(self, _thread_id: str) -> None:
        self.thread = None


class _FakeClient:
    def __init__(self, crons: _FakeCrons) -> None:
        self.crons = crons
        self.threads = _FakeThreads()


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(verify_sweep_cron, "langgraph_client", lambda: client)


async def test_absent_creates_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _FakeCrons()
    _patch_client(monkeypatch, _FakeClient(crons))

    result = await verify_sweep_cron.ensure_verify_sweep_cron()

    assert result == "created:new-cron-id"
    assert crons.search_calls == [{"metadata": {"kind": "verify_sweep"}, "limit": 1}]
    assert len(crons.create_calls) == 1
    args, kwargs = crons.create_calls[0]
    assert args == ("scheduler",)
    assert kwargs == {
        "schedule": "17 * * * *",
        "input": {},
        "config": {"configurable": {"task": "verify_sweep"}},
        "metadata": {"kind": "verify_sweep"},
    }


async def test_present_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _FakeCrons(existing=[{"cron_id": "existing", "metadata": {"kind": "verify_sweep"}}])
    _patch_client(monkeypatch, _FakeClient(crons))

    result = await verify_sweep_cron.ensure_verify_sweep_cron()

    assert result == "existing:existing"
    assert crons.create_calls == []


async def test_second_boot_creates_none(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _FakeCrons()
    _patch_client(monkeypatch, _FakeClient(crons))

    await verify_sweep_cron.ensure_verify_sweep_cron()
    # Store now has the cron; a second boot sees it and does nothing.
    crons.existing = [{"cron_id": "new-cron-id", "metadata": {"kind": "verify_sweep"}}]
    await verify_sweep_cron.ensure_verify_sweep_cron()

    assert len(crons.create_calls) == 1


async def test_concurrent_boots_create_once(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _FakeCrons(block_create=True)
    _patch_client(monkeypatch, _FakeClient(crons))

    first = asyncio.create_task(verify_sweep_cron.ensure_verify_sweep_cron())
    await crons.create_started.wait()
    second = await verify_sweep_cron.ensure_verify_sweep_cron()
    crons.release_create.set()

    assert await first == "created:new-cron-id"
    assert second == "pending"
    assert len(crons.create_calls) == 1


async def test_creation_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    crons = _FakeCrons(fail=True)
    _patch_client(monkeypatch, _FakeClient(crons))

    result = await verify_sweep_cron.ensure_verify_sweep_cron()

    assert result is None
    assert crons.create_calls == []


async def test_schedule_helper_swallows_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crons = _FakeCrons(fail=True)
    _patch_client(monkeypatch, _FakeClient(crons))

    verify_sweep_cron.schedule_verify_sweep_cron_ensure()
    await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})


async def test_operator_script_lists_every_cron_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = [
        {
            "cron_id": f"cron-{index:03}",
            "metadata": {"kind": "verify_sweep" if index == 150 else "other"},
            "schedule": "17 * * * *",
        }
        for index in range(205)
    ]
    crons = _FakeCrons(existing=existing)
    monkeypatch.setattr(ensure_verify_sweep_cron, "_client", lambda: _FakeClient(crons))

    result = await ensure_verify_sweep_cron.list_crons()

    expected_ids = [cron["cron_id"] for cron in existing]
    assert crons.search_calls == [
        {"limit": 100, "offset": 0},
        {"limit": 100, "offset": 100},
        {"limit": 100, "offset": 200},
    ]
    assert [cron["cron_id"] for cron in result] == expected_ids
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == expected_ids
    assert result[150]["metadata"]["kind"] == "verify_sweep"


async def test_operator_script_reports_empty_cron_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    crons = _FakeCrons()
    monkeypatch.setattr(ensure_verify_sweep_cron, "_client", lambda: _FakeClient(crons))

    result = await ensure_verify_sweep_cron.list_crons()

    assert crons.search_calls == [{"limit": 100, "offset": 0}]
    assert result == []
    assert capsys.readouterr().out == "no crons registered\n"
