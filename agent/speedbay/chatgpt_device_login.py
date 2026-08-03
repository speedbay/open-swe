"""ChatGPT device-code login against the current deviceauth contract (OPE-81).

SPEEDBAY ORG-LAYER FILE. This module supersedes
``langchain_openai.chatgpt_oauth.login_chatgpt_device`` (broken as of 1.3.5
and still in 1.4.1, verified 2026-08-03) until upstream adopts the JSON
contract: OpenAI's ``/api/accounts/deviceauth/*`` endpoints now reject
form-encoded bodies with 400, renamed the start-response fields to
``device_auth_id``/``user_code``/``interval``, signal pending as HTTP 403
with ``error.code == "deviceauth_authorization_pending"``, and return a
server-generated PKCE ``code_verifier`` on success. Contract reference:
``codex-rs/login/src/device_code_auth.rs`` in openai/codex; live endpoint
behavior recorded on the OPE-81 ticket:
https://linear.app/speed-bay/issue/OPE-81/restore-chatgpt-device-login-against-the-new-deviceauth-contract

The installed package is imported, never modified: constants, the token
model (``_token_from_response``), and the persistence seam
(``_FileChatGPTOAuthTokenProvider``) all come from
``langchain_openai.chatgpt_oauth``, so the token store this login writes is
consumed by ``agent/speedbay/subscription_auth.py`` unchanged.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import httpx
from langchain_openai.chatgpt_oauth import (
    CHATGPT_CLIENT_ID,
    CHATGPT_DEVICE_CODE_URL,
    CHATGPT_DEVICE_TOKEN_URL,
    CHATGPT_TOKEN_URL,
    DEFAULT_SCOPE,
    DEFAULT_STORE_PATH,
    _FileChatGPTOAuthTokenProvider,
    _post_form,
    _raise_for_oauth_response,
    _token_from_response,
)

from agent.speedbay.config import (
    DEFAULT_CHATGPT_DEVICE_TIMEOUT_SECONDS as DEFAULT_TIMEOUT_SECONDS,
)
from agent.speedbay.config import DEVICE_REDIRECT_URI, DEVICE_VERIFICATION_URL

_PENDING_ERROR_CODE = "deviceauth_authorization_pending"


def _post_json(url: str, body: dict[str, str], *, timeout: float = 30.0) -> dict[str, Any]:
    """POST a JSON payload and return the parsed JSON body."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=body, headers={"Accept": "application/json"})
    _raise_for_oauth_response(url, resp)
    return resp.json()


def _post_device_poll_json(
    url: str, body: dict[str, str], *, timeout: float = 30.0
) -> dict[str, Any] | None:
    """POST a device-code poll; return ``None`` while authorization is pending.

    Pending is HTTP 403 with ``error.code == "deviceauth_authorization_pending"``
    (the new contract) or a bare ``"authorization_pending"`` code. Any other
    >=400 response is terminal and raises.
    """
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=body, headers={"Accept": "application/json"})
    if resp.status_code < 400:
        return resp.json()
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = (error.get("code") if isinstance(error, dict) else error) or (
        payload.get("code") if isinstance(payload, dict) else None
    )
    if resp.status_code == 403 and code == _PENDING_ERROR_CODE:
        return None
    if code == "authorization_pending":
        return None
    _raise_for_oauth_response(url, resp)
    return None


def login_chatgpt_device(
    *,
    store_path: Path | None = None,
    client_id: str = CHATGPT_CLIENT_ID,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> _FileChatGPTOAuthTokenProvider:
    """Run the ChatGPT device-code OAuth flow against the current endpoints.

    Args:
        store_path: Where to persist the token. Defaults to
            `DEFAULT_STORE_PATH`.
        client_id: OAuth client ID (defaults to Codex/ChatGPT client).
        timeout: Total seconds to wait for user authorization. The user code
            itself expires after ~15 minutes.

    Returns:
        A configured `_FileChatGPTOAuthTokenProvider`.

    Raises:
        RuntimeError: The start response was missing required fields, or
            device authorization failed with a terminal error.
        TimeoutError: Authorization was not completed within `timeout` seconds.
    """
    start = _post_json(
        CHATGPT_DEVICE_CODE_URL,
        {"client_id": client_id, "scope": DEFAULT_SCOPE},
    )
    device_auth_id = start.get("device_auth_id")
    user_code = start.get("user_code")
    if not (device_auth_id and user_code):
        msg = "ChatGPT device-auth start response missing required fields."
        raise RuntimeError(msg)
    try:
        interval = float(start.get("interval") or 5)
    except (TypeError, ValueError):
        interval = 5.0
    if not math.isfinite(interval) or interval < 0:
        interval = 5.0

    print(  # noqa: T201
        "\nFollow these steps to sign in with ChatGPT using device code "
        f"authorization:\n\n1. Open this link in your browser and sign in to your "
        f"account\n   {DEVICE_VERIFICATION_URL}\n\n2. Enter this one-time code "
        f"(expires in 15 minutes)\n   {user_code}\n\nContinue only if you started "
        "this login here. If a website or another person gave you this code, cancel."
    )

    deadline = time.monotonic() + timeout
    success: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        success = _post_device_poll_json(
            CHATGPT_DEVICE_TOKEN_URL,
            {"device_auth_id": device_auth_id, "user_code": user_code},
            timeout=max(deadline - time.monotonic(), 0.0),
        )
        if success is not None:
            break
        time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))
    if success is None:
        msg = "Timed out waiting for ChatGPT device authorization."
        raise TimeoutError(msg)

    code_verifier = success.get("code_verifier")
    authorization_code = success.get("authorization_code")
    if not (code_verifier and authorization_code):
        msg = "ChatGPT device-auth success response missing required fields."
        raise RuntimeError(msg)

    # The token exchange stays form-encoded (the OAuth token endpoint never
    # changed); only the deviceauth endpoints moved to JSON. The code_verifier
    # is server-generated — the client no longer sends a code_challenge.
    response = _post_form(
        CHATGPT_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": DEVICE_REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
    )
    token = _token_from_response(response)
    provider = _FileChatGPTOAuthTokenProvider(
        path=store_path or DEFAULT_STORE_PATH, client_id=client_id
    )
    provider.save(token)
    return provider


__all__ = ["DEVICE_REDIRECT_URI", "DEVICE_VERIFICATION_URL", "login_chatgpt_device"]
