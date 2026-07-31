"""Tests for the Linear self-trigger guard (OPE-23).

The fixture is a real captured Linear webhook delivery (a service-bot-authored
comment on OPE-21; author fields renamed to the current swe-service-bot
account, ids as captured), so the payload shape is authoritative, not invented.

Run:  .venv/bin/python -m pytest tests/speedbay/test_linear_guard.py -x -q
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

from agent.speedbay import linear_guard as guard

FIXTURE = pathlib.Path(__file__).parent / "linear_comment_payload.json"
PAYLOAD = json.loads(FIXTURE.read_text())
AUTHOR_ID = PAYLOAD["actor"]["id"]


def _run(coro):
    return asyncio.run(coro)


async def _fixed_viewer(value):
    return value


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The viewer id caches for the process; tests need isolation."""
    guard._resolved = False
    guard._cached_id = None
    yield
    guard._resolved = False
    guard._cached_id = None


def test_self_comment_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_viewer_id", lambda: _fixed_viewer(AUTHOR_ID))
    assert _run(guard.is_self_comment(PAYLOAD)) is True


def test_human_comment_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_viewer_id", lambda: _fixed_viewer("some-other-user-id"))
    assert _run(guard.is_self_comment(PAYLOAD)) is False


def test_matches_data_userid_when_actor_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """actor and data.userId are separate carriers; either alone must match."""
    monkeypatch.setattr(guard, "_viewer_id", lambda: _fixed_viewer(AUTHOR_ID))
    stripped = copy.deepcopy(PAYLOAD)
    del stripped["actor"]
    assert stripped["data"]["userId"] == AUTHOR_ID  # fixture sanity
    assert _run(guard.is_self_comment(stripped)) is True


def test_fail_open_when_viewer_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No viewer id (key missing / Linear down) must never block human comments."""
    monkeypatch.setattr(guard, "_viewer_id", lambda: _fixed_viewer(None))
    assert _run(guard.is_self_comment(PAYLOAD)) is False


def test_viewer_id_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert _run(guard._viewer_id()) is None


def test_transient_failure_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed resolution must retry on the next delivery, not stick for life."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    class Boom:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            raise ConnectionError("linear down")

        async def __aexit__(self, *a): ...

    monkeypatch.setattr(guard.httpx, "AsyncClient", Boom)
    assert _run(guard._viewer_id()) is None
    assert guard._resolved is False  # next call retries


def test_failed_call_uses_concurrent_siblings_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing first call must not fail open when a concurrent call resolved."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")

    class SiblingWinsThenBoom:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            guard._resolved = True  # concurrent sibling resolved first
            guard._cached_id = AUTHOR_ID
            raise ConnectionError("this call's own request failed")

        async def __aexit__(self, *a): ...

    monkeypatch.setattr(guard.httpx, "AsyncClient", SiblingWinsThenBoom)
    assert _run(guard._viewer_id()) == AUTHOR_ID


def test_empty_payload_is_not_self(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_viewer_id", lambda: _fixed_viewer(AUTHOR_ID))
    assert _run(guard.is_self_comment({})) is False


# --- trigger-owner scope (OPE-36) --------------------------------------------


def _comment_from(email: str | None) -> dict:
    user = {"email": email} if email is not None else {}
    return {"data": {"user": user, "body": "@openswe go"}}


def test_unscoped_instance_accepts_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSWE_TRIGGER_OWNER_EMAILS", raising=False)
    assert guard.is_foreign_comment(_comment_from("anyone@speedbay.com")) is False
    assert guard.is_foreign_comment(_comment_from(None)) is False


def test_scoped_instance_accepts_only_its_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSWE_TRIGGER_OWNER_EMAILS", "sbrown@speedbay.com")
    assert guard.is_foreign_comment(_comment_from("sbrown@speedbay.com")) is False
    assert guard.is_foreign_comment(_comment_from("SBrown@Speedbay.com")) is False  # case
    assert guard.is_foreign_comment(_comment_from("tkelley@speedbay.com")) is True


def test_scoped_instance_supports_multiple_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSWE_TRIGGER_OWNER_EMAILS", "a@x.com, b@x.com")
    assert guard.is_foreign_comment(_comment_from("b@x.com")) is False
    assert guard.is_foreign_comment(_comment_from("c@x.com")) is True


def test_scoped_instance_drops_missing_email_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSWE_TRIGGER_OWNER_EMAILS", "sbrown@speedbay.com")
    assert guard.is_foreign_comment(_comment_from(None)) is True
    assert guard.is_foreign_comment(_comment_from("")) is True
    assert guard.is_foreign_comment({}) is True


# --- duplicate-delivery dedup (OPE-56) -----------------------------------------

COMMENT_ID = PAYLOAD["data"]["id"]


@pytest.fixture(autouse=True)
def _fresh_seen_comments():
    """The dedup set lives for the process; tests need isolation."""
    guard._seen_comment_ids.clear()
    yield
    guard._seen_comment_ids.clear()


@pytest.fixture
def route_call(monkeypatch: pytest.MonkeyPatch):
    """One delivery through linear_webhook with everything external stubbed."""
    from agent.webhooks import linear_routes

    profile_repo = None

    async def _none(*_args, **_kwargs):
        return None

    async def _login(*_args, **_kwargs):
        return "cbass-speedbay"

    async def _profile_repo(*_args, **_kwargs):
        return profile_repo

    monkeypatch.setattr(linear_routes.common, "verify_linear_signature", lambda *_args: True)
    monkeypatch.setattr(linear_routes.common, "fetch_linear_issue_details", _none)
    monkeypatch.setattr(linear_routes.speedbay_linear_guard, "is_self_comment", _none)
    monkeypatch.setattr(
        linear_routes.common, "extract_repo_from_text", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(linear_routes.common, "resolve_login_from_email_async", _login)
    monkeypatch.setattr(linear_routes.common, "get_profile_default_repo", _profile_repo)
    monkeypatch.setattr(
        linear_routes.common, "get_repo_config_from_team_mapping", lambda *_args: None
    )
    monkeypatch.setattr(linear_routes.common, "get_team_default_repo", _none)
    monkeypatch.setattr(linear_routes.common, "_is_repo_allowed", lambda *_args: True)

    def _call(payload: dict, bg_tasks, *, resolve_repo: bool = True):
        nonlocal profile_repo
        profile_repo = {"owner": "speedbay", "name": "warehouse"} if resolve_repo else None

        async def _body():
            return json.dumps(payload).encode()

        request = SimpleNamespace(
            body=_body,
            headers={"Linear-Signature": "valid"},
        )
        return asyncio.run(linear_routes.linear_webhook(request, bg_tasks))

    return _call


class _CapturingBackgroundTasks:
    def __init__(self) -> None:
        self.calls: list = []

    def add_task(self, fn, *args, **kwargs) -> None:
        self.calls.append((fn, args))


def _triggering_payload(comment_id: str | None) -> dict:
    payload = copy.deepcopy(PAYLOAD)
    payload["data"]["body"] = "@openswe implement"
    if comment_id is None:
        del payload["data"]["id"]
    else:
        payload["data"]["id"] = comment_id
    return payload


def test_guard_same_comment_id_twice_second_is_duplicate() -> None:
    assert guard.is_duplicate_comment(PAYLOAD) is False
    assert guard.is_duplicate_comment(copy.deepcopy(PAYLOAD)) is True


def test_guard_distinct_comment_ids_both_pass() -> None:
    other = _triggering_payload("bbbb-cccc-dddd-eeee")
    assert guard.is_duplicate_comment(PAYLOAD) is False
    assert guard.is_duplicate_comment(other) is False


def test_guard_missing_comment_id_fails_open() -> None:
    assert guard.is_duplicate_comment({}) is False
    assert guard.is_duplicate_comment({"data": {}}) is False
    assert guard.is_duplicate_comment({"data": {"id": "  "}}) is False


def test_guard_bounded_fifo_eviction() -> None:
    for i in range(513):
        assert guard.is_duplicate_comment({"data": {"id": f"c-{i}"}}) is False
    assert guard.is_duplicate_comment({"data": {"id": "c-0"}}) is False
    assert guard.is_duplicate_comment({"data": {"id": "c-512"}}) is True


def test_route_replaying_captured_payload_dispatches_exactly_once(route_call) -> None:
    """AC: the same comment delivered twice (once per covering webhook) starts
    exactly one run — no interrupted twin."""
    bg = _CapturingBackgroundTasks()
    first = route_call(_triggering_payload(COMMENT_ID), bg)
    second = route_call(_triggering_payload(COMMENT_ID), bg)
    assert first["status"] == "accepted"
    assert second["status"] == "ignored"
    assert "Duplicate" in second["reason"]
    assert len(bg.calls) == 1


def test_route_distinct_comments_on_same_issue_both_dispatch(route_call) -> None:
    bg = _CapturingBackgroundTasks()
    first = route_call(_triggering_payload(COMMENT_ID), bg)
    second = route_call(_triggering_payload("bbbb-cccc-dddd-eeee"), bg)
    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    assert len(bg.calls) == 2


def test_route_ignored_delivery_does_not_poison_retry(route_call) -> None:
    bg = _CapturingBackgroundTasks()
    payload = _triggering_payload(COMMENT_ID)
    first = route_call(payload, bg, resolve_repo=False)
    second = route_call(payload, bg)
    assert first == {"status": "ignored", "reason": "No default repository configured"}
    assert second["status"] == "accepted"
    assert len(bg.calls) == 1


def test_route_missing_comment_id_still_dispatches(route_call) -> None:
    bg = _CapturingBackgroundTasks()
    first = route_call(_triggering_payload(None), bg)
    second = route_call(_triggering_payload(None), bg)
    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    assert len(bg.calls) == 2
