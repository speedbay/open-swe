"""Host-only atomic agent-default model settings operation (OPE-134)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph_sdk import get_client
from pydantic import BaseModel, ConfigDict

from ..dashboard.options import (
    FABLE_MODEL_IDS,
    SUPPORTED_MODEL_IDS,
    gate_fable_model,
    model_supports_effort,
)
from ..dashboard.team_settings import (
    TEAM_SETTINGS_KEY,
    TEAM_SETTINGS_NAMESPACE,
    get_team_default_model,
)
from ..utils import ttl_cache

team_settings_commit_lock = asyncio.Lock()


def _require_host_client(request: Request) -> None:
    """Host-only enforcement in code, not just proxy config: this route changes
    the workspace-wide model defaults, so only a direct loopback client (the
    host CLI) may call it. Proxied traffic always carries X-Forwarded-For
    (Caddy and cloudflared append it), so it is rejected even if the prefix
    were ever added to the public proxy."""
    client = request.client
    if (
        client is None
        or client.host not in ("127.0.0.1", "::1")
        or "x-forwarded-for" in request.headers
    ):
        raise HTTPException(403, "host-only route")


model_settings_router = APIRouter(
    prefix="/speedbay/model-settings",
    tags=["speedbay-model-settings"],
    dependencies=[Depends(_require_host_client)],
)


class AgentDefaultModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_id: str
    effort: str


class EffectiveModelPair(BaseModel):
    model_id: str
    effort: str


class AgentDefaultModelResponse(BaseModel):
    main: EffectiveModelPair
    subagent: EffectiveModelPair


def _client():
    return get_client()


def _item_value(item: object) -> dict[str, Any]:
    value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
    return dict(value) if isinstance(value, dict) else {}


def _cache_keys() -> tuple[str, str, str]:
    from .. import server

    return (
        f"team-default-model-pair:agent:{id(server.get_team_default_model_pair)}",
        f"team:fable-enabled:{id(server.get_team_fable_enabled)}",
        # agent.chat._cached_team_chat_model caches get_team_default_model("chat"),
        # which inherits the agent default when no chat-specific model is set.
        f"team-default-model:chat:{id(get_team_default_model)}",
    )


async def commit_agent_default_model(update: AgentDefaultModelUpdate) -> AgentDefaultModelResponse:
    if update.model_id not in SUPPORTED_MODEL_IDS:
        raise HTTPException(400, f"unsupported selectable model: {update.model_id}")
    if not model_supports_effort(update.model_id, update.effort):
        raise HTTPException(400, f"unsupported effort {update.effort!r} for {update.model_id!r}")

    async with team_settings_commit_lock:
        store = _client().store
        value = _item_value(await store.get_item(TEAM_SETTINGS_NAMESPACE, TEAM_SETTINGS_KEY))
        fable_enabled = value.get("fable_enabled") is True
        if update.model_id in FABLE_MODEL_IDS and not fable_enabled:
            raise HTTPException(400, "Fable is disabled for this workspace")

        value.update(
            {
                "default_agent_model": update.model_id,
                "default_agent_reasoning_effort": update.effort,
                "default_agent_subagent_model": update.model_id,
                "default_agent_subagent_reasoning_effort": update.effort,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        await store.put_item(TEAM_SETTINGS_NAMESPACE, TEAM_SETTINGS_KEY, value)

        for cache_key in _cache_keys():
            ttl_cache.invalidate(cache_key)
        from .. import server

        (main, subagent), effective_fable_enabled = await asyncio.gather(
            server._cached_team_default_model_pair("agent"), server._cached_fable_enabled()
        )
        main = gate_fable_model(*main, fable_enabled=effective_fable_enabled)
        subagent = gate_fable_model(*subagent, fable_enabled=effective_fable_enabled)
        requested = (update.model_id, update.effort)
        if main != requested or subagent != requested:
            raise HTTPException(
                500, "stored agent defaults did not resolve to the requested model pair"
            )
        # main == subagent == requested was verified above, so the typed
        # update fields are the effective pair.
        pair = EffectiveModelPair(model_id=update.model_id, effort=update.effort)
        return AgentDefaultModelResponse(main=pair, subagent=pair)


@model_settings_router.put("/agent-default", response_model=AgentDefaultModelResponse)
async def put_agent_default_model(update: AgentDefaultModelUpdate) -> AgentDefaultModelResponse:
    return await commit_agent_default_model(update)
