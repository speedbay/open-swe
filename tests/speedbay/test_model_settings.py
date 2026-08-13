"""Regression tests for the host-only atomic model-settings commit (OPE-134)."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest
from fastapi import HTTPException

from agent.dashboard import team_settings
from agent.speedbay import model_settings
from agent.utils import ttl_cache
from speedbay import set_model

MODEL = "openai:gpt-5.6-sol"
EFFORT = "medium"


class FakeStore:
    def __init__(self, value: dict[str, Any] | None = None) -> None:
        self.value = deepcopy(value) if value is not None else None
        self.writes: list[dict[str, Any]] = []
        self.dashboard_write_started = asyncio.Event()
        self.release_dashboard_write = asyncio.Event()
        self.block_dashboard_write = False

    async def get_item(self, _namespace: list[str], _key: str) -> dict[str, Any] | None:
        return {"value": deepcopy(self.value)} if self.value is not None else None

    async def put_item(self, _namespace: list[str], _key: str, value: dict[str, Any]) -> None:
        if self.block_dashboard_write and value.get("org_guidelines") == "dashboard update":
            self.dashboard_write_started.set()
            await self.release_dashboard_write.wait()
        self.value = deepcopy(value)
        self.writes.append(deepcopy(value))


class FakeClient:
    def __init__(self, store: FakeStore) -> None:
        self.store = store


@pytest.fixture()
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    store = FakeStore({"fable_enabled": False, "unrelated": "preserve"})
    client = FakeClient(store)
    monkeypatch.setattr(model_settings, "_client", lambda: client)
    monkeypatch.setattr(team_settings, "_client", lambda: client)
    ttl_cache.clear()
    return store


async def _commit(model_id: str = MODEL, effort: str = EFFORT):
    return await model_settings.put_agent_default_model(
        model_settings.AgentDefaultModelUpdate(model_id=model_id, effort=effort)
    )


async def test_commit_preserves_forced_concurrent_unrelated_update(fake_store: FakeStore) -> None:
    fake_store.block_dashboard_write = True
    dashboard = asyncio.create_task(
        team_settings.upsert_team_settings(
            team_settings.TeamSettingsUpdate(org_guidelines="dashboard update")
        )
    )
    await fake_store.dashboard_write_started.wait()
    commit_started = asyncio.Event()

    async def commit_model() -> None:
        commit_started.set()
        await _commit()

    commit = asyncio.create_task(commit_model())
    await commit_started.wait()
    assert not commit.done()
    fake_store.release_dashboard_write.set()
    await asyncio.gather(dashboard, commit)

    assert len(fake_store.writes) == 2
    assert fake_store.writes[0]["org_guidelines"] == "dashboard update"
    assert fake_store.writes[1]["org_guidelines"] == "dashboard update"
    assert fake_store.writes[1]["default_agent_model"] == MODEL


async def test_commit_writes_one_complete_model_value(fake_store: FakeStore) -> None:
    await _commit()

    assert len(fake_store.writes) == 1
    written = fake_store.writes[0]
    assert {
        field: written[field]
        for field in (
            "default_agent_model",
            "default_agent_reasoning_effort",
            "default_agent_subagent_model",
            "default_agent_subagent_reasoning_effort",
        )
    } == {
        "default_agent_model": MODEL,
        "default_agent_reasoning_effort": EFFORT,
        "default_agent_subagent_model": MODEL,
        "default_agent_subagent_reasoning_effort": EFFORT,
    }
    assert written["unrelated"] == "preserve"


async def test_commit_rejects_unsupported_id_without_write(fake_store: FakeStore) -> None:
    with pytest.raises(HTTPException, match="unsupported selectable model") as exc_info:
        await _commit("unknown:model")
    assert exc_info.value.status_code == 400
    assert fake_store.writes == []


async def test_commit_rejects_invalid_effort_without_write(fake_store: FakeStore) -> None:
    with pytest.raises(HTTPException, match="unsupported effort") as exc_info:
        await _commit(MODEL, "invalid")
    assert exc_info.value.status_code == 400
    assert fake_store.writes == []


async def test_commit_rejects_disabled_fable_without_write(fake_store: FakeStore) -> None:
    with pytest.raises(HTTPException, match="Fable is disabled") as exc_info:
        await _commit("anthropic:claude-fable-5", "high")
    assert exc_info.value.status_code == 400
    assert fake_store.writes == []


async def test_commit_replaces_warmed_effective_runtime_pair(fake_store: FakeStore) -> None:
    from agent import server

    fake_store.value = {
        "fable_enabled": False,
        "default_agent_model": "anthropic:claude-opus-5",
        "default_agent_reasoning_effort": "high",
        "default_agent_subagent_model": "anthropic:claude-opus-5",
        "default_agent_subagent_reasoning_effort": "high",
    }
    old_main, old_subagent = await server._cached_team_default_model_pair("agent")
    response = await _commit()
    new_main, new_subagent = await server._cached_team_default_model_pair("agent")

    assert (old_main, old_subagent) != ((MODEL, EFFORT), (MODEL, EFFORT))
    assert (new_main, new_subagent) == ((MODEL, EFFORT), (MODEL, EFFORT))
    assert response.main.model_dump() == {"model_id": MODEL, "effort": EFFORT}
    assert response.subagent.model_dump() == {"model_id": MODEL, "effort": EFFORT}


def test_cli_nonzero_on_failed_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_request(path: str, body: dict, method: str = "POST") -> dict | None:
        raise SystemExit(f"{path} failed: 400 b'unsupported selectable model'")

    monkeypatch.setattr(set_model, "_request", fail_request)
    monkeypatch.setattr(set_model.sys, "argv", ["set_model.py", "unknown:model"])

    with pytest.raises(SystemExit) as exc_info:
        set_model.main()
    assert exc_info.value.code != 0
    assert "unsupported selectable model" in str(exc_info.value)
    assert capsys.readouterr().out == ""


def test_cli_sends_pair_and_prints_effective_pairs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict, str]] = []

    def request(path: str, body: dict, method: str = "POST") -> dict:
        calls.append((path, body, method))
        return {
            "main": {"model_id": MODEL, "effort": EFFORT},
            "subagent": {"model_id": MODEL, "effort": EFFORT},
        }

    monkeypatch.setattr(set_model, "_request", request)
    monkeypatch.setattr(set_model.sys, "argv", ["set_model.py", MODEL, EFFORT])

    set_model.main()

    assert calls == [
        (
            "/speedbay/model-settings/agent-default",
            {"model_id": MODEL, "effort": EFFORT},
            "PUT",
        )
    ]
    assert capsys.readouterr().out.splitlines() == [
        f"effective main agent: {MODEL} (effort={EFFORT})",
        f"effective subagent: {MODEL} (effort={EFFORT})",
    ]
