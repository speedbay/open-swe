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


async def test_invalidate_discards_in_flight_stale_load() -> None:
    """A cache load started before invalidate() must not repopulate the key,
    so the commit's post-write verification read always loads fresh state."""
    ttl_cache.clear()
    release = asyncio.Event()

    async def stale_loader() -> str:
        await release.wait()
        return "stale"

    in_flight = asyncio.create_task(ttl_cache.cached("k", 60, stale_loader))
    await asyncio.sleep(0)  # loader is now awaiting release
    ttl_cache.invalidate("k")
    release.set()
    assert await in_flight == "stale"

    async def fresh_loader() -> str:
        return "fresh"

    assert await ttl_cache.cached("k", 60, fresh_loader) == "fresh"


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


async def test_commit_invalidates_inherited_chat_default(fake_store: FakeStore) -> None:
    """Chat inherits the agent default when no chat-specific model is set, so a
    commit must also invalidate the cached chat default."""
    from agent import chat

    fake_store.value = {
        "fable_enabled": False,
        "default_agent_model": "anthropic:claude-opus-5",
        "default_agent_reasoning_effort": "high",
    }
    assert await chat._cached_team_chat_model() == ("anthropic:claude-opus-5", "high")
    await _commit()
    assert await chat._cached_team_chat_model() == (MODEL, EFFORT)


async def test_dashboard_partial_update_preserves_committed_defaults(
    fake_store: FakeStore,
) -> None:
    """An upsert that omits the model fields must not null out a committed
    agent default."""
    await _commit()
    await team_settings.upsert_team_settings(
        team_settings.TeamSettingsUpdate(org_guidelines="only guidelines")
    )

    stored = fake_store.value
    assert stored is not None
    assert stored["org_guidelines"] == "only guidelines"
    assert stored["default_agent_model"] == MODEL
    assert stored["default_agent_subagent_reasoning_effort"] == EFFORT
    assert stored["unrelated"] == "preserve"


async def test_partial_fable_disable_converts_stored_fable_defaults(
    fake_store: FakeStore,
) -> None:
    """The ZDR kill switch must act on the merged record: disabling Fable in a
    partial update converts Fable defaults that only exist in the store."""
    fable = "anthropic:claude-fable-5"
    fake_store.value = {
        "fable_enabled": True,
        "default_agent_model": fable,
        "default_agent_reasoning_effort": "high",
        "default_chat_model": fable,
        "default_chat_reasoning_effort": "high",
    }
    await team_settings.upsert_team_settings(team_settings.TeamSettingsUpdate(fable_enabled=False))

    stored = fake_store.value
    assert stored is not None
    assert stored["fable_enabled"] is False
    assert stored["default_agent_model"] != fable
    assert stored["default_chat_model"] != fable


async def test_upsert_response_reflects_committed_write_without_reread(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PUT response is built from the committed value, so a store read
    failure after a successful write cannot mask it behind defaults."""
    original_get_item = FakeStore.get_item
    writes_seen = len(fake_store.writes)

    async def get_item_fails_after_write(self: FakeStore, namespace: list[str], key: str):
        if len(self.writes) > writes_seen:
            raise RuntimeError("transient store read failure")
        return await original_get_item(self, namespace, key)

    monkeypatch.setattr(FakeStore, "get_item", get_item_fails_after_write)
    saved = await team_settings.upsert_team_settings(
        team_settings.TeamSettingsUpdate(org_guidelines="still visible")
    )
    assert saved["org_guidelines"] == "still visible"


async def test_route_rejects_non_host_clients(fake_store: FakeStore) -> None:
    """The route is host-only: only direct loopback clients, never proxied
    (X-Forwarded-For) or remote traffic."""
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(model_settings.model_settings_router)
    body = {"model_id": MODEL, "effort": EFFORT}
    path = "/speedbay/model-settings/agent-default"

    async def put(client_addr: tuple[str, int], headers: dict[str, str] | None = None):
        transport = httpx.ASGITransport(app=app, client=client_addr)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.put(path, json=body, headers=headers)

    assert (await put(("127.0.0.1", 1234))).status_code == 200
    assert (await put(("10.0.0.5", 1234))).status_code == 403
    assert (
        await put(("127.0.0.1", 1234), headers={"x-forwarded-for": "203.0.113.9"})
    ).status_code == 403


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
