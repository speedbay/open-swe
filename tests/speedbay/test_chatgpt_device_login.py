"""ChatGPT device-code login against the current deviceauth contract (OPE-81).

Behavioral tests for `agent/speedbay/chatgpt_device_login.py`: httpx is
monkeypatched with canned responses — no live network, no real credentials.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_openai.chatgpt_oauth import (
    CHATGPT_CLIENT_ID,
    CHATGPT_DEVICE_CODE_URL,
    CHATGPT_DEVICE_TOKEN_URL,
    CHATGPT_TOKEN_URL,
    DEFAULT_SCOPE,
    _FileChatGPTOAuthTokenProvider,
)

from agent.speedbay import chatgpt_device_login
from agent.speedbay.chatgpt_device_login import DEVICE_REDIRECT_URI, login_chatgpt_device

_START_RESPONSE = {"device_auth_id": "dev-auth-1", "user_code": "ABCD-1234", "interval": 0}
_PENDING_RESPONSE = {"error": {"code": "deviceauth_authorization_pending"}}
_SUCCESS_RESPONSE = {
    "authorization_code": "auth-code-1",
    "code_challenge": "server-challenge",
    "code_verifier": "server-verifier",
}


def _b64url(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def _fake_id_token() -> str:
    claims = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-1",
            "chatgpt_plan_type": "plus",
            "chatgpt_user_id": "user-1",
        }
    }
    return f"{_b64url({'alg': 'none'})}.{_b64url(claims)}.sig"


_TOKEN_RESPONSE = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "id_token": _fake_id_token(),
    "expires_in": 3600,
}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Stand-in for `httpx.Client` recording request kwargs per URL."""

    def __init__(self, script: dict[str, list[dict[str, Any]]], calls: list[dict[str, Any]]):
        self._script = script
        self._calls = calls

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeClient:
        return self

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._calls.append({"url": url, **kwargs})
        step = self._script[url].pop(0)
        return _FakeResponse(step["status"], step["body"])


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a scripted httpx.Client and patch out poll sleeping."""
    script: dict[str, list[dict[str, Any]]] = {
        CHATGPT_DEVICE_CODE_URL: [{"status": 200, "body": _START_RESPONSE}],
        CHATGPT_DEVICE_TOKEN_URL: [
            {"status": 403, "body": _PENDING_RESPONSE},
            {"status": 200, "body": _SUCCESS_RESPONSE},
        ],
        CHATGPT_TOKEN_URL: [{"status": 200, "body": _TOKEN_RESPONSE}],
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(chatgpt_device_login.httpx, "Client", _FakeClient(script, calls))
    monkeypatch.setattr(chatgpt_device_login.time, "sleep", lambda _: None)
    return {"script": script, "calls": calls}


def test_start_request_is_json_with_exact_fields(http: dict[str, Any], tmp_path: Path) -> None:
    login_chatgpt_device(store_path=tmp_path / "chatgpt-auth.json")

    start = http["calls"][0]
    assert start["url"] == CHATGPT_DEVICE_CODE_URL
    assert start["json"] == {"client_id": CHATGPT_CLIENT_ID, "scope": DEFAULT_SCOPE}
    assert "data" not in start


def test_poll_is_json_and_pending_403_loops_to_success(
    http: dict[str, Any], tmp_path: Path
) -> None:
    login_chatgpt_device(store_path=tmp_path / "chatgpt-auth.json")

    polls = [c for c in http["calls"] if c["url"] == CHATGPT_DEVICE_TOKEN_URL]
    assert len(polls) == 2
    for poll in polls:
        assert poll["json"] == {"device_auth_id": "dev-auth-1", "user_code": "ABCD-1234"}
        assert "data" not in poll


def test_exchange_uses_server_verifier_and_deviceauth_redirect(
    http: dict[str, Any], tmp_path: Path
) -> None:
    login_chatgpt_device(store_path=tmp_path / "chatgpt-auth.json")

    exchange = http["calls"][-1]
    assert exchange["url"] == CHATGPT_TOKEN_URL
    assert exchange["data"] == {
        "grant_type": "authorization_code",
        "code": "auth-code-1",
        "redirect_uri": DEVICE_REDIRECT_URI,
        "client_id": CHATGPT_CLIENT_ID,
        "code_verifier": "server-verifier",
    }


def test_tokens_persist_through_provider(http: dict[str, Any], tmp_path: Path) -> None:
    store_path = tmp_path / "chatgpt-auth.json"

    provider = login_chatgpt_device(store_path=store_path)

    assert isinstance(provider, _FileChatGPTOAuthTokenProvider)
    stored = json.loads(store_path.read_text())
    assert stored["access_token"] == "access-1"
    assert stored["refresh_token"] == "refresh-1"
    assert stored["account_id"] == "acct-1"


def test_terminal_poll_error_raises(http: dict[str, Any], tmp_path: Path) -> None:
    http["script"][CHATGPT_DEVICE_TOKEN_URL] = [
        {"status": 400, "body": {"error": {"code": "invalid_request"}}}
    ]

    with pytest.raises(RuntimeError, match="400"):
        login_chatgpt_device(store_path=tmp_path / "chatgpt-auth.json")
