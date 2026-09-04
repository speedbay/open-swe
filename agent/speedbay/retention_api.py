"""Atomic stale-thread deletion inside the in-memory LangGraph runtime."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from langgraph_runtime_inmem.checkpoint import Checkpointer
from langgraph_runtime_inmem.database import GLOBAL_STORE
from pydantic import BaseModel

retention_router = APIRouter(prefix="/internal/thread-retention", tags=["thread-retention"])
DELETABLE_STATUSES = {"idle", "error"}


class DeleteStaleThreadRequest(BaseModel):
    thread_id: UUID
    cutoff: datetime


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def delete_stale_thread(thread_id: UUID, cutoff: datetime) -> bool:
    """Atomically recheck and delete one stale thread from the in-memory runtime."""
    cutoff = cutoff.replace(tzinfo=UTC) if cutoff.tzinfo is None else cutoff.astimezone(UTC)
    thread = next(
        (item for item in GLOBAL_STORE["threads"] if item["thread_id"] == thread_id), None
    )
    updated_at = _timestamp(thread.get("updated_at")) if thread else None
    if (
        thread is None
        or thread.get("status") not in DELETABLE_STATUSES
        or updated_at is None
        or updated_at >= cutoff
        or any(str(cron.get("thread_id")) == str(thread_id) for cron in GLOBAL_STORE["crons"])
    ):
        return False

    GLOBAL_STORE["threads"].remove(thread)
    GLOBAL_STORE["runs"] = [run for run in GLOBAL_STORE["runs"] if run["thread_id"] != thread_id]
    GLOBAL_STORE["crons"] = [
        cron for cron in GLOBAL_STORE["crons"] if str(cron.get("thread_id")) != str(thread_id)
    ]
    checkpointer = Checkpointer()
    thread_key = str(thread_id)
    checkpointer.storage.pop(thread_key, None)
    checkpointer.writes = defaultdict(
        dict, {key: value for key, value in checkpointer.writes.items() if key[0] != thread_key}
    )
    return True


@retention_router.post("/delete")
async def delete_stale_thread_route(
    payload: DeleteStaleThreadRequest, request: Request
) -> dict[str, bool]:
    host = request.client.host if request.client else ""
    try:
        is_loopback = ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="thread retention is loopback-only")
    return {"deleted": delete_stale_thread(payload.thread_id, payload.cutoff)}
