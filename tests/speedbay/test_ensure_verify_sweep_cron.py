"""Tests for verify-sweep cron provisioning."""

from __future__ import annotations

from typing import Any

import pytest

from speedbay import ensure_verify_sweep_cron


class _FakeCrons:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self.create_calls = 0

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        return [{"cron_id": "existing", "metadata": {"kind": "verify_sweep"}}]

    async def create(self, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        self.create_calls += 1
        return {"cron_id": "duplicate"}


class _FakeClient:
    def __init__(self) -> None:
        self.crons = _FakeCrons()


async def test_create_searches_by_metadata_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(ensure_verify_sweep_cron, "_client", lambda: client)

    await ensure_verify_sweep_cron.create()

    assert client.crons.search_calls == [
        {"metadata": {"kind": ensure_verify_sweep_cron.KIND}, "limit": 1}
    ]
    assert client.crons.create_calls == 0
