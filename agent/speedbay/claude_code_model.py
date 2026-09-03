"""Claude subscription OAuth: token provider + ``ChatAnthropic`` subclass (OPE-61).

SPEEDBAY ORG-LAYER FILE. Upstream does not own this module; it is reached only
through the ``anthropic:`` branch of ``agent/speedbay/subscription_auth.py``
(itself registered in ``make_model`` by OPE-60). No upstream file changes.

Wire contract (read from pi's installed source, recorded in OPE-61): Claude
subscription OAuth tokens are accepted only when the request looks like Claude
Code — bearer auth instead of ``x-api-key``, the ``claude-code-20250219`` +
``oauth-2025-04-20`` beta flags, a ``claude-cli`` user-agent with ``x-app:
cli``, and a ``system`` array whose FIRST block is exactly
:data:`CLAUDE_CODE_IDENTITY` (the caller's real system prompt follows as the
second block). Tool names need no renaming.

Tokens come from Claude Code's own credential store (macOS Keychain entry
``Claude Code-credentials``, file fallback ``~/.claude/.credentials.json``).
Refresh tokens ROTATE on use, so every refresh writes the rotated pair back to
the store — dropping it would brick both this deployment and Claude Code.

This is an unofficial subscription-token path, same ToS posture as
pi-claude-auth; the ``SPEEDBAY_SUBSCRIPTION_AUTH`` toggle keeps API-key
billing the default (decision recorded in OPE-61).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anthropic import Omit
from langchain_anthropic import ChatAnthropic
from pydantic import Field, model_validator

if TYPE_CHECKING:
    from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatGenerationChunk, ChatResult

logger = logging.getLogger(__name__)

CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
_OAUTH_BETAS = ("claude-code-20250219", "oauth-2025-04-20")
_CLAUDE_CLI_VERSION = "2.1.75"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CREDENTIAL_SOURCE_KEY = "speedbayCredentialSource"
_KEYCHAIN_WRITE_FALLBACK = "keychain-write-fallback"
_REFRESH_SKEW_SECONDS = 300.0
_BEARER_KWARG = "_claude_bearer"


def _default_credentials_path() -> Path:
    """Claude Code's file credential store (the non-Keychain fallback)."""
    return Path.home() / ".claude" / ".credentials.json"


def _default_use_keychain() -> bool:
    """Whether to prefer the macOS Keychain source (seam for tests)."""
    return sys.platform == "darwin"


def _valid_marked_credentials(creds: dict[str, Any]) -> bool:
    if not all(
        isinstance(creds.get(key), str) and creds[key] for key in ("accessToken", "refreshToken")
    ):
        return False
    try:
        expires_at = float(creds.get("expiresAt") or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(expires_at)


@dataclass
class ClaudeCodeTokenProvider:
    """Refresh-aware reader/writer for Claude Code's OAuth credential store.

    Mirrors langchain-openai's ``_FileChatGPTOAuthTokenProvider`` design:
    expiry-skewed refresh, cross-process file lock around the file store's
    read-refresh-write, atomic private writes. The Keychain source is read
    with the ``security`` CLI; write-back failures there fall back to the
    file store rather than dropping a rotated refresh token.
    """

    # Late-bound through lambdas so tests can monkeypatch the module seams.
    path: Path = field(default_factory=lambda: _default_credentials_path())
    use_keychain: bool = field(default_factory=lambda: _default_use_keychain())

    def read(self) -> tuple[dict[str, Any], str]:
        """Return ``(claudeAiOauth credentials, source)``; raise when unreadable."""
        try:
            wrapper = json.loads(self.path.read_text())
            creds = wrapper.get("claudeAiOauth")
            if (
                wrapper.get(_CREDENTIAL_SOURCE_KEY) == _KEYCHAIN_WRITE_FALLBACK
                and isinstance(creds, dict)
                and _valid_marked_credentials(creds)
            ):
                return creds, _KEYCHAIN_WRITE_FALLBACK
        except Exception:
            pass
        if self.use_keychain:
            try:
                proc = subprocess.run(
                    ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
                return json.loads(proc.stdout.decode())["claudeAiOauth"], "keychain"
            except Exception as exc:
                logger.debug("Keychain read failed (%s); trying %s", exc, self.path)
        return json.loads(self.path.read_text())["claudeAiOauth"], "file"

    def get_token(self) -> str:
        """Return a live access token, refreshing (and writing back) near expiry.

        The refresh cycle is serialized under the cross-process file lock for
        BOTH sources: refresh tokens rotate on use, so two workers refreshing
        the same expiring credential concurrently would brick the second
        exchange. The lock file is a machine-wide mutex independent of where
        the credentials themselves live; the re-read under the lock keeps the
        re-read's source so the rotated pair is written back where it came from.
        """
        creds, _ = self.read()
        if not self._expiring(creds):
            return str(creds["accessToken"])
        with self._file_lock():
            creds, source = self.read()  # another process may have refreshed already
            if not self._expiring(creds):
                return str(creds["accessToken"])
            return self._refresh(creds, source)

    async def aget_token(self) -> str:
        """Async ``get_token``; refresh HTTP runs off the event loop."""
        return await asyncio.to_thread(self.get_token)

    @staticmethod
    def _expiring(creds: dict[str, Any]) -> bool:
        """Whether the access token is within the refresh skew of ``expiresAt`` (ms)."""
        expires_at = float(creds.get("expiresAt") or 0) / 1000.0
        return time.time() >= expires_at - _REFRESH_SKEW_SECONDS

    def _refresh(self, creds: dict[str, Any], source: str) -> str:
        """Exchange the refresh token, persist the rotated pair, return the access token."""
        import httpx

        response = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds["refreshToken"],
                "client_id": _CLIENT_ID,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        token = response.json()
        updated = {
            **creds,
            "accessToken": token["access_token"],
            "refreshToken": token.get("refresh_token") or creds["refreshToken"],
            "expiresAt": int((time.time() + float(token["expires_in"])) * 1000),
        }
        self._write_back(updated, source)
        return str(updated["accessToken"])

    def _write_back(self, creds: dict[str, Any], source: str) -> None:
        """Persist rotated credentials to their source store."""
        if source == "keychain":
            try:
                subprocess.run(
                    [
                        "security",
                        "add-generic-password",
                        "-U",
                        "-a",
                        os.environ.get("USER", ""),
                        "-s",
                        _KEYCHAIN_SERVICE,
                        "-w",
                        json.dumps({"claudeAiOauth": creds}),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
                return
            except Exception as exc:
                # Refresh tokens rotate on use: losing the rotated pair bricks
                # the store, so persist to the file fallback instead of dropping.
                self.use_keychain = False
                source = _KEYCHAIN_WRITE_FALLBACK
                logger.warning(
                    "Keychain write-back failed (%s); writing %s",
                    type(exc).__name__,
                    self.path,
                )
        wrapper: dict[str, Any] = {"claudeAiOauth": creds}
        if source == _KEYCHAIN_WRITE_FALLBACK:
            wrapper[_CREDENTIAL_SOURCE_KEY] = _KEYCHAIN_WRITE_FALLBACK
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # Create with 0600 before any secret bytes hit disk; write-then-chmod
        # leaves a umask-default-readable window.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(wrapper))
        os.replace(tmp, self.path)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Cross-process exclusive lock guarding the file store's refresh cycle."""
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class ChatClaudeCode(ChatAnthropic):
    """``ChatAnthropic`` authenticated by Claude subscription OAuth tokens.

    Every request is reshaped to the Claude Code wire contract in
    ``_get_request_payload``: bearer ``authorization`` from a fresh token,
    ``x-api-key`` removed (:class:`anthropic.Omit`), the OAuth beta flags
    merged ahead of any request betas, the ``claude-cli`` identity headers,
    and :data:`CLAUDE_CODE_IDENTITY` prepended as the first ``system`` block.
    The async paths prefetch the token off the event loop (mirroring
    ``_ChatOpenAICodex``'s private-kwarg design) so a mid-run refresh never
    blocks the loop. All other ``ChatAnthropic`` behavior is inherited.
    """

    token_provider: Any = Field(default=None, exclude=True)
    """Refresh-aware token source; defaults to :class:`ClaudeCodeTokenProvider`."""

    @model_validator(mode="before")
    @classmethod
    def _apply_oauth_defaults(cls, values: dict[str, Any]) -> dict[str, Any]:
        """Default the token provider and satisfy the SDK's api-key plumbing.

        The placeholder ``api_key`` only keeps client construction happy; the
        ``x-api-key`` header it produces is removed on every request.
        """
        if not isinstance(values, dict):
            return values
        if values.get("token_provider") is None:
            values["token_provider"] = ClaudeCodeTokenProvider()
        values.setdefault("api_key", "speedbay-subscription-oauth")
        return values

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Build the payload, then apply the Claude Code OAuth wire contract."""
        token = kwargs.pop(_BEARER_KWARG, None) or self.token_provider.get_token()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        system = payload.get("system")
        # ponytail: identity block carries no cache_control; caller blocks keep theirs.
        blocks: list[dict[str, Any]] = [{"type": "text", "text": CLAUDE_CODE_IDENTITY}]
        if isinstance(system, str) and system:
            blocks.append({"type": "text", "text": system})
        elif isinstance(system, list):
            blocks.extend(system)
        payload["system"] = blocks

        request_betas = [str(beta) for beta in payload.get("betas") or []]
        flags = list(dict.fromkeys([*_OAUTH_BETAS, *request_betas]))
        # Exact SDK casing is load-bearing: anthropic's _build_headers merges
        # header dicts case-SENSITIVELY (its own comment) and emits the auth
        # header as "X-Api-Key" — a lowercase Omit strips nothing and the
        # placeholder key reaches the server (observed live 401, 2026-08-02).
        oauth_headers: dict[str, Any] = {
            "Authorization": f"Bearer {token}",
            "X-Api-Key": Omit(),
            "anthropic-beta": ",".join(flags),
            "user-agent": f"claude-cli/{_CLAUDE_CLI_VERSION}",
            "x-app": "cli",
        }
        payload["extra_headers"] = {**(payload.get("extra_headers") or {}), **oauth_headers}
        return payload

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        kwargs.setdefault(_BEARER_KWARG, await self.token_provider.aget_token())
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        kwargs.setdefault(_BEARER_KWARG, await self.token_provider.aget_token())
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk
