"""Boot-time provisioning for the hourly verify-sweep cron (OPE-53).

SPEEDBAY org-layer file — upstream does not own it.

OPE-42 left cron creation to an operator script run once per deployment; no
PR test can prove that step ran. ``ensure_verify_sweep_cron`` moves that step
server-side: the backend ensures the cron idempotently at boot (invoked from
the docker branch of ``validate_sandbox_startup_config`` — FORK.md
registration #3), so a fresh deployment needs zero cron provisioning.

Idempotent by metadata and a server-side provisioning lock: concurrent boots
cannot create duplicate crons. Failure logs loudly but must not block boot — the OPE-39
webhook fast path works without the sweep, so a missing cron degrades to
missed-event recovery only.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..utils.thread_ops import langgraph_client

logger = logging.getLogger(__name__)

KIND = "verify_sweep"
ASSISTANT_ID = "scheduler"
# Minute 17, hourly: fixed offset avoids the top-of-hour thundering herd.
SCHEDULE = "17 * * * *"
LOCK_THREAD_ID = "0cfcdca5-e5aa-53ef-9f62-d374b3763a58"


async def ensure_verify_sweep_cron() -> str | None:
    """Ensure the hourly verify-sweep cron exists; create it only when absent.

    Returns the cron id ("existing:<id>" when already present, "created:<id>"
    when newly created), ``"pending"`` when another boot owns the provisioning
    lock, or None when the ensure failed. Never raises: any error is logged and
    swallowed so boot is never blocked by cron provisioning.
    """
    try:
        client = langgraph_client()
        existing = await client.crons.search(metadata={"kind": KIND}, limit=1)
        if existing:
            cron_id = str(existing[0].get("cron_id", ""))
            logger.info("verify_sweep cron already provisioned: %s", cron_id)
            return f"existing:{cron_id}"

        owner = str(uuid.uuid4())
        lock: Any = await client.threads.create(
            thread_id=LOCK_THREAD_ID,
            if_exists="do_nothing",
            metadata={"kind": f"{KIND}_provisioning", "owner": owner},
            ttl=1,
        )
        lock_metadata = lock.get("metadata", {}) if isinstance(lock, dict) else {}
        if lock_metadata.get("owner") != owner:
            logger.info("verify_sweep cron provisioning already in progress")
            return "pending"
        try:
            cron: Any = await client.crons.create(
                ASSISTANT_ID,
                schedule=SCHEDULE,
                input={},
                config={"configurable": {"task": KIND}},
                metadata={"kind": KIND},
            )
        finally:
            await client.threads.delete(LOCK_THREAD_ID)
        cron_id = str(cron.get("cron_id", "")) if isinstance(cron, dict) else ""
        logger.info("verify_sweep cron created: %s schedule=%s", cron_id, SCHEDULE)
        return f"created:{cron_id}"
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to ensure verify_sweep cron; boot continues "
            "(webhook fast path unaffected, sweep recovery unavailable until next boot)"
        )
        return None


def schedule_verify_sweep_cron_ensure() -> None:
    """Kick off ``ensure_verify_sweep_cron`` as a background task at boot."""
    import asyncio

    asyncio.get_running_loop().create_task(ensure_verify_sweep_cron())
