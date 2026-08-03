"""Boot-time provisioning for the nightly subscription-auth health cron (OPE-68)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..utils.thread_ops import langgraph_client

logger = logging.getLogger(__name__)
KIND = "subscription_health"
SCHEDULE = "0 13 * * *"
LOCK_THREAD_ID = "d0f08d52-36e9-56a7-8961-03a2956b988d"


async def ensure_subscription_health_cron() -> str | None:
    """Ensure the nightly health cron exists without blocking boot on failure."""
    try:
        client = langgraph_client()
        existing = await client.crons.search(metadata={"kind": KIND}, limit=1)
        if existing:
            return f"existing:{existing[0].get('cron_id', '')}"
        owner = str(uuid.uuid4())
        lock: Any = await client.threads.create(
            thread_id=LOCK_THREAD_ID,
            if_exists="do_nothing",
            metadata={"kind": f"{KIND}_provisioning", "owner": owner},
            ttl=1,
        )
        if not isinstance(lock, dict) or lock.get("metadata", {}).get("owner") != owner:
            return "pending"
        try:
            cron: Any = await client.crons.create(
                "scheduler",
                schedule=SCHEDULE,
                input={},
                config={"configurable": {"task": KIND}},
                metadata={"kind": KIND},
            )
        finally:
            await client.threads.delete(LOCK_THREAD_ID)
        cron_id = cron.get("cron_id", "") if isinstance(cron, dict) else ""
        logger.info("subscription_health cron created: %s schedule=%s", cron_id, SCHEDULE)
        return f"created:{cron_id}"
    except Exception:  # noqa: BLE001
        logger.exception("Failed to ensure subscription_health cron; boot continues")
        return None


def schedule_subscription_health_cron_ensure() -> None:
    """Kick off the subscription-health cron ensure as a background boot task."""
    import asyncio

    asyncio.get_running_loop().create_task(ensure_subscription_health_cron())
