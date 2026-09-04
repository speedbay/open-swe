#!/usr/bin/env python3
"""Delete Open SWE threads that have exceeded the idle retention window."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph_sdk import get_client

DEFAULT_RETENTION_DAYS = 10
PAGE_SIZE = 100
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


async def _cron_thread_ids(client: Any) -> set[str]:
    thread_ids: set[str] = set()
    offset = 0
    while True:
        page = await client.crons.search(
            limit=PAGE_SIZE,
            offset=offset,
            select=["thread_id"],
        )
        for cron in page:
            thread_id = cron.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                thread_ids.add(thread_id)
        if len(page) < PAGE_SIZE:
            return thread_ids
        offset += len(page)


async def sweep(
    client: Any, *, now: datetime | None = None, days: int | None = None
) -> dict[str, Any]:
    """Delete qualifying threads and return summary counts."""
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    cutoff = current_time.astimezone(UTC) - timedelta(days=days or retention_days())
    cron_thread_ids = await _cron_thread_ids(client)
    candidates: list[str] = []
    scanned = 0
    offset = 0

    while True:
        page = await client.threads.search(
            limit=PAGE_SIZE,
            offset=offset,
            sort_by="updated_at",
            sort_order="asc",
            select=["thread_id", "updated_at", "status"],
        )
        scanned += len(page)
        for thread in page:
            thread_id = thread.get("thread_id")
            updated_at = _timestamp(thread.get("updated_at"))
            status = thread.get("status")
            if (
                isinstance(thread_id, str)
                and thread_id
                and status in DELETABLE_STATUSES
                and updated_at is not None
                and updated_at < cutoff
                and thread_id not in cron_thread_ids
            ):
                candidates.append(thread_id)
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)

    for thread_id in candidates:
        await client.threads.delete(thread_id)

    summary = {
        "scanned": scanned,
        "deleted": len(candidates),
        "skipped": scanned - len(candidates),
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


async def main() -> None:
    await sweep(get_client(url="http://127.0.0.1:2024"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
