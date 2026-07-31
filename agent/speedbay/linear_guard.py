"""Deterministic self-trigger guard for the Linear webhook (OPE-23).

SPEEDBAY org-layer file — upstream does not own it.

The agent posts its Linear replies with the same runtime ``LINEAR_API_KEY``
that can trigger runs, and comments authored with a plain API key arrive with
``botActor: null`` — the route's bot filter never catches them (verified live;
FORK.md § Linear trigger). Without this guard, the only things preventing a
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

Duplicate-delivery dedup (OPE-56): Linear delivers each event once per
covering webhook (two for OPE — verified live during OPE-39), and the comment
route dispatched on every delivery, so each duplicate spawned an ``interrupted``
twin run the same second. ``is_duplicate_comment`` keys a process-lifetime
bounded FIFO set on the comment's ``data.id`` — comments are create-only, so
one id is delivered once per webhook and never legitimately recurs. Mirrors
``_seen_transitions`` in ``verify_trigger.py``; multi-worker deployments fall
back to the thread-level interrupt semantics, as documented there.
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


# Process-lifetime duplicate-delivery guard (OPE-56). Same sizing and FIFO
# eviction as ``_seen_transitions`` in verify_trigger.py; a set, not a map,
# because the comment id alone is the dedup key — there is no per-id value.
_SEEN_COMMENTS_MAX = 512
_seen_comment_ids: dict[str, None] = {}


def is_duplicate_comment(payload: dict[str, Any]) -> bool:
    """True when this comment id was already dispatched by this process.

    Missing/blank id fails open (not duplicate): such a delivery cannot be
    distinguished from a fresh comment, so the thread-level interrupt
    semantics absorb any duplicate instead of dropping real triggers.
    """
    comment_id = (payload.get("data") or {}).get("id")
    if not isinstance(comment_id, str) or not comment_id.strip():
        return False
    if comment_id in _seen_comment_ids:
        return True
    _seen_comment_ids[comment_id] = None
    while len(_seen_comment_ids) > _SEEN_COMMENTS_MAX:
        _seen_comment_ids.pop(next(iter(_seen_comment_ids)))
    return False


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
