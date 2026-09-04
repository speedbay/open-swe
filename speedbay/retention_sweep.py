#!/usr/bin/env python3
"""Delete Open SWE threads that have exceeded the idle retention window."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langgraph_sdk import get_client

DEFAULT_RETENTION_DAYS = 10
PAGE_SIZE = 100
DELETE_PATH = "/internal/thread-retention/delete"
DELETABLE_STATUSES = {"idle", "error"}
logger = logging.getLogger(__name__)


def retention_days() -> int:
    """Return the configured positive retention window in days."""
    value = os.environ.get("OPENSWE_THREAD_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        days = int(value)
    except ValueError as exc:
        raise ValueError("OPENSWE_THREAD_RETENTION_DAYS must be a positive integer") from exc
    if days <= 0:
        raise ValueError("OPENSWE_THREAD_RETENTION_DAYS must be a positive integer")
    return days


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _is_deletable(thread: dict[str, Any], cutoff: datetime) -> bool:
    updated_at = _timestamp(thread.get("updated_at"))
    return (
        thread.get("status") in DELETABLE_STATUSES
        and updated_at is not None
        and updated_at < cutoff
    )


async def sweep(
    client: Any,
    delete_thread: Callable[[str, datetime], Awaitable[bool]],
    *,
    now: datetime | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Delete qualifying threads and return summary counts."""
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    retention = retention_days() if days is None else days
    if retention <= 0:
        raise ValueError("retention days must be a positive integer")
    cutoff = current_time.astimezone(UTC) - timedelta(days=retention)
    candidates: list[str] = []
    scanned = 0
    offset = 0
    initial_count = await client.threads.count()

    while offset < initial_count:
        page = await client.threads.search(
            limit=min(PAGE_SIZE, initial_count - offset),
            offset=offset,
            sort_by="created_at",
            sort_order="asc",
            select=["thread_id", "updated_at", "status"],
        )
        scanned += len(page)
        for thread in page:
            thread_id = thread.get("thread_id")
            if isinstance(thread_id, str) and thread_id and _is_deletable(thread, cutoff):
                candidates.append(thread_id)
        if not page:
            break
        offset += len(page)

    deleted = sum([await delete_thread(thread_id, cutoff) for thread_id in candidates])

    summary = {
        "scanned": scanned,
        "deleted": deleted,
        "skipped": scanned - deleted,
        "cutoff": cutoff,
    }
    logger.info(
        "Open SWE thread retention sweep: scanned=%d deleted=%d skipped=%d cutoff=%s",
        summary["scanned"],
        summary["deleted"],
        summary["skipped"],
        cutoff.date().isoformat(),
    )
    return summary


async def _delete_thread(http: httpx.AsyncClient, thread_id: str, cutoff: datetime) -> bool:
    response = await http.post(
        DELETE_PATH,
        json={"thread_id": thread_id, "cutoff": cutoff.isoformat()},
    )
    response.raise_for_status()
    return response.json()["deleted"] is True


async def main() -> None:
    url = os.environ.get("LANGGRAPH_URL") or os.environ.get(
        "LANGGRAPH_URL_PROD", "http://127.0.0.1:2024"
    )
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2024") as http:
        await sweep(
            get_client(url=url), lambda thread_id, cutoff: _delete_thread(http, thread_id, cutoff)
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
