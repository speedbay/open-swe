#!/usr/bin/env python3
"""List/inspect registered crons, e.g. the verify-sweep cron (OPE-42/OPE-53).

The verify-sweep cron is now provisioned idempotently by the backend at boot
(``agent/speedbay/verify_sweep_cron.py``); this script is inspection only —
its old ``--create`` provisioning path is gone.

    speedbay/ensure_verify_sweep_cron.py   # list existing crons
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langgraph_sdk import get_client

KIND = "verify_sweep"


def _client():
    url = os.environ.get("LANGGRAPH_URL") or os.environ.get(
        "LANGGRAPH_URL_PROD", "http://localhost:2024"
    )
    return get_client(url=url)


async def list_crons() -> list[Any]:
    crons: list[Any] = []
    offset = 0
    while True:
        page = await _client().crons.search(limit=100, offset=offset)
        if not page:
            break
        crons.extend(page)
        if len(page) < 100:
            break
        offset += len(page)
    if not crons:
        print("no crons registered")
    for c in crons:
        cid = str(c.get("cron_id", ""))[:8]
        kind = (c.get("metadata") or {}).get("kind", "(no kind)")
        print(f"  {cid}  {kind:20} schedule={c.get('schedule')}")
    return crons


if __name__ == "__main__":
    asyncio.run(list_crons())
