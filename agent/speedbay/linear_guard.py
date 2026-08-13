"""Deterministic self-trigger and delivery guards for Linear webhooks.

SPEEDBAY org-layer file — upstream does not own it.

The agent posts its Linear replies with the same runtime ``LINEAR_API_KEY``
that can trigger runs, and comments authored with a plain API key arrive with
``botActor: null`` — the route's bot filter never catches them (verified live;
OPERATIONS.md § Linear trigger). Without this guard, the only things preventing a
self-trigger loop are upstream's known-prefix list (which does not match our
agent's free-form replies) and the model happening not to write ``@openswe``
in a reply. One quoted trigger instruction in a completion comment would spawn
runs from runs, each billing a full agent run.

The fix is the standard bot-loop defense: resolve the runtime key's own user
id once, and drop any webhook comment authored by that id.

Async on purpose: the guard runs inside the async webhook route, where
langgraph dev's blockbuster instrumentation raises ``BlockingError`` on any
sync socket call — a sync HTTP client here doesn't just degrade the event
loop, it *fails* and silently disables the guard (found live in the first
verification attempt).

Fail-open by design: if the viewer id cannot be resolved (key missing or
Linear unreachable), the guard reports "not self" so human comments are never
blocked by an outage — the pre-guard state was permanently fail-open anyway.

Linear can deliver one create event to each covering webhook. Delivery claims
are therefore process-local and keyed by the comment's nonblank ``data.id``.
A claim stays pending through dispatch, succeeds only after a normal dispatch
result, and releases on an explicit ``False`` result or a raised failure.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .config import LINEAR_GQL_URL, trigger_owner_emails

logger = logging.getLogger(__name__)


# Process-lifetime cache: (resolved-flag, viewer id). The runtime key does not
# rotate while the server is up; a restart clears it. Concurrent first calls
# may both fetch — same result, no lock needed.
_resolved = False
_cached_id: str | None = None


async def _viewer_id() -> str | None:
    """The runtime ``LINEAR_API_KEY``'s own Linear user id, or None."""
    global _resolved, _cached_id
    if _resolved:
        return _cached_id

    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        logger.warning("self-trigger guard disabled: LINEAR_API_KEY is not set")
        _resolved = True
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                LINEAR_GQL_URL,
                json={"query": "{ viewer { id } }"},
                headers={"Authorization": key, "Content-Type": "application/json"},
            )
        _cached_id = resp.json()["data"]["viewer"]["id"]
    except Exception:
        # Not cached: a transient Linear outage at boot shouldn't disable the
        # guard for the process lifetime — retry on the next delivery.
        logger.warning(
            "self-trigger guard: could not resolve viewer id (will retry)", exc_info=True
        )
        # A concurrent first call may have resolved the viewer while this one
        # failed; use its result instead of failing open (PR #8 review).
        if _resolved:
            return _cached_id
        return None
    _resolved = True
    logger.info("self-trigger guard active for Linear user %s", _cached_id)
    return _cached_id


_SEEN_COMMENTS_MAX = 512
_comment_delivery_states: dict[str, str] = {}


async def dispatch_comment_once(payload: dict[str, Any], dispatcher, *args, **kwargs) -> None:
    """Dispatch a comment once while its process-local claim is pending or succeeded."""
    comment_id = (payload.get("data") or {}).get("id")
    if not isinstance(comment_id, str) or not comment_id.strip():
        await dispatcher(*args, **kwargs)
        return

    if comment_id in _comment_delivery_states:
        return

    if len(_comment_delivery_states) == _SEEN_COMMENTS_MAX:
        for seen_id, state in _comment_delivery_states.items():
            if state == "succeeded":
                del _comment_delivery_states[seen_id]
                break
        else:
            await dispatcher(*args, **kwargs)
            return

    _comment_delivery_states[comment_id] = "pending"
    try:
        if await dispatcher(*args, **kwargs) is False:
            del _comment_delivery_states[comment_id]
            return
    except BaseException:
        del _comment_delivery_states[comment_id]
        raise
    _comment_delivery_states[comment_id] = "succeeded"


async def is_self_comment(payload: dict[str, Any]) -> bool:
    """True when the webhook comment was authored by the runtime key itself.

    Checks both id carriers observed in real deliveries: top-level
    ``actor.id`` and ``data.userId``.
    """
    viewer = await _viewer_id()
    if viewer is None:
        return False
    author_ids = {
        (payload.get("actor") or {}).get("id"),
        (payload.get("data") or {}).get("userId"),
    }
    return viewer in author_ids


def is_foreign_comment(payload: dict[str, Any]) -> bool:
    """True when this instance's trigger-owner scope excludes the comment.

    Unscoped (``OPENSWE_TRIGGER_OWNER_EMAILS`` unset/empty) accepts everyone —
    single-instance behavior. Scoped, the comment author's email must match
    one owner email (case-insensitive); a scoped instance with no author
    email in the payload drops the comment (fail-closed) rather than risk
    two instances acting on it.
    """
    owners = trigger_owner_emails()
    if not owners:
        return False
    email = ((payload.get("data") or {}).get("user") or {}).get("email")
    if not isinstance(email, str) or not email.strip():
        logger.info("trigger-owner scope: no author email in payload — dropping")
        return True
    return email.strip().lower() not in owners
