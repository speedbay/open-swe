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

import pytest
from starlette.types import Message

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


# --- retry-safe duplicate-delivery claims (OPE-158) ----------------------------

COMMENT_ID = PAYLOAD["data"]["id"]


@pytest.fixture(autouse=True)
def _fresh_comment_delivery_states():
    guard._comment_delivery_states.clear()
    yield
    guard._comment_delivery_states.clear()


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

    async def _call(payload: dict, bg_tasks, *, resolve_repo: bool = True):
        nonlocal profile_repo
        profile_repo = {"owner": "speedbay", "name": "warehouse"} if resolve_repo else None

        async def _receive() -> Message:
            return {"type": "http.request", "body": json.dumps(payload).encode()}

        request = linear_routes.common.Request(
            {"type": "http", "headers": [(b"linear-signature", b"valid")]},
            _receive,
        )
        return await linear_routes.linear_webhook(request, bg_tasks)

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


async def test_dispatch_comment_once_exception_releases_for_retry() -> None:
    calls = 0

    error = RuntimeError("dispatch failed")

    async def raises() -> None:
        nonlocal calls
        calls += 1
        raise error

    payload = _triggering_payload(COMMENT_ID)
    with pytest.raises(RuntimeError) as raised:
        await guard.dispatch_comment_once(payload, raises)
    assert raised.value is error
    assert COMMENT_ID not in guard._comment_delivery_states

    with pytest.raises(RuntimeError) as retried:
        await guard.dispatch_comment_once(payload, raises)
    assert retried.value is error
    assert calls == 2


async def test_dispatch_comment_once_false_releases_for_retry() -> None:
    calls = 0

    async def false_then_success() -> bool | None:
        nonlocal calls
        calls += 1
        return False if calls == 1 else None

    payload = _triggering_payload(COMMENT_ID)
    await guard.dispatch_comment_once(payload, false_then_success)
    assert COMMENT_ID not in guard._comment_delivery_states

    await guard.dispatch_comment_once(payload, false_then_success)
    assert calls == 2
    assert guard._comment_delivery_states[COMMENT_ID] == "succeeded"


async def test_route_concurrent_duplicate_claims_once_and_commits_success(
    monkeypatch: pytest.MonkeyPatch, route_call
) -> None:
    from agent.webhooks import linear_routes

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def dispatcher(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(linear_routes.service, "process_linear_issue", dispatcher)
    first_bg = _CapturingBackgroundTasks()
    second_bg = _CapturingBackgroundTasks()
    payload = _triggering_payload(COMMENT_ID)
    first, second = await asyncio.gather(
        route_call(payload, first_bg),
        route_call(copy.deepcopy(payload), second_bg),
    )
    assert first["status"] == second["status"] == "accepted"
    assert len(first_bg.calls) == len(second_bg.calls) == 1

    first_call, second_call = first_bg.calls[0], second_bg.calls[0]
    tasks = [
        asyncio.create_task(first_call[0](*first_call[1])),
        asyncio.create_task(second_call[0](*second_call[1])),
    ]
    await entered.wait()
    assert calls == 1
    assert guard._comment_delivery_states[COMMENT_ID] == "pending"
    release.set()
    await asyncio.gather(*tasks)
    assert guard._comment_delivery_states[COMMENT_ID] == "succeeded"

    late_bg = _CapturingBackgroundTasks()
    late = await route_call(copy.deepcopy(payload), late_bg)
    assert late["status"] == "accepted"
    late_call = late_bg.calls[0]
    await late_call[0](*late_call[1])
    assert calls == 1


async def test_dispatch_comment_once_identity_and_capacity_boundaries() -> None:
    calls: list[str] = []

    async def dispatch(label: str) -> None:
        calls.append(label)

    await guard.dispatch_comment_once(_triggering_payload("first"), dispatch, "first")
    await guard.dispatch_comment_once(_triggering_payload("second"), dispatch, "second")
    await guard.dispatch_comment_once({"data": {}}, dispatch, "missing")
    await guard.dispatch_comment_once({"data": {"id": "  "}}, dispatch, "blank")
    assert calls == ["first", "second", "missing", "blank"]
    assert set(guard._comment_delivery_states) == {"first", "second"}

    guard._comment_delivery_states.clear()
    for i in range(guard._SEEN_COMMENTS_MAX):
        guard._comment_delivery_states[f"succeeded-{i}"] = "succeeded"
    await guard.dispatch_comment_once(_triggering_payload("new"), dispatch, "new")
    assert "succeeded-0" not in guard._comment_delivery_states
    assert guard._comment_delivery_states["new"] == "succeeded"

    guard._comment_delivery_states.clear()
    guard._comment_delivery_states["pending"] = "pending"
    for i in range(guard._SEEN_COMMENTS_MAX - 1):
        guard._comment_delivery_states[f"succeeded-{i}"] = "succeeded"
    await guard.dispatch_comment_once(_triggering_payload("newer"), dispatch, "newer")
    assert guard._comment_delivery_states["pending"] == "pending"
    assert "succeeded-0" not in guard._comment_delivery_states

    guard._comment_delivery_states.clear()
    for i in range(guard._SEEN_COMMENTS_MAX):
        guard._comment_delivery_states[f"pending-{i}"] = "pending"
    await guard.dispatch_comment_once(_triggering_payload("untracked"), dispatch, "untracked")
    assert "untracked" not in guard._comment_delivery_states
    assert calls[-1] == "untracked"
