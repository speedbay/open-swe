#!/usr/bin/env python3
"""Idempotently provision the hourly verify-sweep cron (OPE-42).

Registers a cron on the ``scheduler`` graph whose tick runs
``agent/speedbay/verify_sweep.py`` (``configurable.task == "verify_sweep"``),
re-dispatching issues stuck in ready-for-verify after a missed webhook.

Operator-run once per deployment against a *running* backend, like
``create_linear_webhook.py``:

    speedbay/ensure_verify_sweep_cron.py            # list existing crons
    speedbay/ensure_verify_sweep_cron.py --create   # create if absent

Idempotent by metadata: an existing cron with ``kind == "verify_sweep"`` is
reported and never duplicated.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from langgraph_sdk import get_client

KIND = "verify_sweep"
ASSISTANT_ID = "scheduler"
# Minute 17, hourly: fixed offset avoids the top-of-hour thundering herd.
SCHEDULE = "17 * * * *"


def _client():
    url = os.environ.get("LANGGRAPH_URL") or os.environ.get(
        "LANGGRAPH_URL_PROD", "http://localhost:2024"
    )
    return get_client(url=url)


async def _existing() -> list[Any]:
    return await _client().crons.search(metadata={"kind": KIND}, limit=1)


async def list_crons() -> None:
    crons = await _client().crons.search(limit=100)
    if not crons:
        print("no crons registered")
    for c in crons:
        cid = str(c.get("cron_id", ""))[:8]
        kind = (c.get("metadata") or {}).get("kind", "(no kind)")
        print(f"  {cid}  {kind:20} schedule={c.get('schedule')}")


async def create() -> None:
    existing = await _existing()
    if existing:
        print(f"already provisioned: cron {existing[0].get('cron_id')} — nothing to do")
        return
    cron = await _client().crons.create(
        ASSISTANT_ID,
        schedule=SCHEDULE,
        input={},
        config={"configurable": {"task": KIND}},
        metadata={"kind": KIND},
    )
    print(f"created: cron {cron.get('cron_id')} schedule={SCHEDULE}")


if __name__ == "__main__":
    asyncio.run(create() if "--create" in sys.argv else list_crons())
