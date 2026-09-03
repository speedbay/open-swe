"""Subscription OAuth model auth (OPE-60, OPE-61).

Structural tests for `agent/speedbay/subscription_auth.py`,
`agent/speedbay/claude_code_model.py`, and the `make_model` registration:
pytest, class-per-concern, no model or network calls. Token stores are temp
files and refresh HTTP is monkeypatched; no real credentials are read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agent.speedbay import claude_code_model, subscription_auth
from agent.speedbay.claude_code_model import (
    CLAUDE_CODE_IDENTITY,
    ChatClaudeCode,
    ClaudeCodeTokenProvider,
)
from agent.speedbay.subscription_auth import ENV_TOGGLE, subscription_model
from agent.utils import model as model_module
from agent.utils.model import make_model

_OPENAI_ID = "openai:gpt-5.6-sol"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Clear the model cache, warn-once memory, and toggle around every test."""
    monkeypatch.delenv(ENV_TOGGLE, raising=False)
    model_module._MODEL_CACHE.clear()
    subscription_auth._warned.clear()
    yield
    model_module._MODEL_CACHE.clear()
    subscription_auth._warned.clear()


@pytest.fixture
def chatgpt_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the ChatGPT token-store seam at an existing temp file."""
    store = tmp_path / "chatgpt-auth.json"
    store.write_text("{}")
    monkeypatch.setattr(subscription_auth, "_chatgpt_store_path", lambda: store)
    return store


@pytest.fixture
def captured_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub `init_chat_model` inside `make_model`, capturing its kwargs."""
    captured: dict[str, Any] = {}

    def fake_init(model: str, **kwargs: Any) -> object:
        captured["model"] = model
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(model_module, "init_chat_model", fake_init)
    return captured


class TestToggleTransitions:
    """AC: every construction follows the current normalized toggle state."""

    def test_disabling_toggle_bypasses_cached_subscription_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscription = object()
        api_key = object()
        monkeypatch.setattr(model_module, "subscription_model", lambda *_: subscription)
        monkeypatch.setattr(model_module, "init_chat_model", lambda **_: api_key)

        monkeypatch.setenv(ENV_TOGGLE, "1")
        assert make_model(_OPENAI_ID) is subscription
        monkeypatch.setenv(ENV_TOGGLE, "0")
        assert make_model(_OPENAI_ID) is api_key

    @pytest.mark.parametrize("enabled", ["1", " TRUE ", "YeS", " on "])
    def test_enabling_toggle_selects_subscription_model(
        self, monkeypatch: pytest.MonkeyPatch, enabled: str
    ) -> None:
        subscription = object()
        api_key = object()
        monkeypatch.setattr(model_module, "subscription_model", lambda *_: subscription)
        monkeypatch.setattr(model_module, "init_chat_model", lambda **_: api_key)

        monkeypatch.setenv(ENV_TOGGLE, "0")
        assert make_model(_OPENAI_ID) is api_key
        monkeypatch.setenv(ENV_TOGGLE, enabled)
        assert make_model(_OPENAI_ID) is subscription

    def test_same_state_reuse_remains_event_loop_isolated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        def build_subscription(*_: object) -> object:
            nonlocal calls
            calls += 1
            return object()

        async def build_model() -> object:
            return make_model(_OPENAI_ID)

        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(model_module, "subscription_model", build_subscription)
        first_loop = asyncio.new_event_loop()
        second_loop = asyncio.new_event_loop()
        try:
            first = first_loop.run_until_complete(build_model())
            assert first_loop.run_until_complete(build_model()) is first
            assert second_loop.run_until_complete(build_model()) is not first
        finally:
            first_loop.close()
            second_loop.close()
        assert calls == 2


class TestDisabledIsByteIdentical:
    """AC: with the toggle unset, make_model behavior is unchanged."""

    def test_subscription_model_returns_none(self) -> None:
        assert subscription_model(_OPENAI_ID, {}) is None

    def test_openai_reaches_init_chat_model_with_todays_kwargs(
        self, captured_init: dict[str, Any]
    ) -> None:
        make_model(_OPENAI_ID, max_tokens=100)
        assert captured_init["model"] == _OPENAI_ID
        assert captured_init["kwargs"] == {
            "max_tokens": 100,
            "max_retries": model_module.DEFAULT_MAX_RETRIES,
            "timeout": model_module.DEFAULT_REQUEST_TIMEOUT_SECONDS,
            "base_url": model_module.OPENAI_RESPONSES_WS_BASE_URL,
            "use_responses_api": True,
            "store": False,
            "output_version": "responses/v1",
            "include": ["reasoning.encrypted_content"],
        }

    def test_anthropic_reaches_init_chat_model_with_todays_kwargs(
        self, captured_init: dict[str, Any]
    ) -> None:
        make_model("anthropic:claude-opus-5", max_tokens=64)
        assert captured_init["model"] == "anthropic:claude-opus-5"
        assert captured_init["kwargs"] == {
            "max_tokens": 64,
            "max_retries": model_module.DEFAULT_MAX_RETRIES,
            "timeout": model_module.DEFAULT_REQUEST_TIMEOUT_SECONDS,
        }


class TestOpenAIBranch:
    """AC: enabled + readable store returns a codex-backend OAuth model."""

    def test_returns_codex_model_pinned_to_chatgpt_backend(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        from langchain_openai.chat_models.codex import (
            CHATGPT_CODEX_BASE_URL,
            _ChatOpenAICodex,
        )

        monkeypatch.setenv(ENV_TOGGLE, "1")
        model = make_model(_OPENAI_ID, max_tokens=100)
        assert isinstance(model, _ChatOpenAICodex)
        assert model.openai_api_base == CHATGPT_CODEX_BASE_URL
        assert model.store is False
        assert model.token_provider.path == chatgpt_store

    def test_model_is_cached_across_calls(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        monkeypatch.setenv(ENV_TOGGLE, "1")
        first = make_model(_OPENAI_ID, max_tokens=100)
        assert make_model(_OPENAI_ID, max_tokens=100) is first

    def test_forced_kwargs_are_dropped_not_conflicting(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        """make_model-style base_url/store kwargs must not reach the pinned model."""
        monkeypatch.setenv(ENV_TOGGLE, "1")
        model = subscription_model(
            _OPENAI_ID,
            {"base_url": "wss://api.openai.com/v1", "store": True, "max_tokens": 10},
        )
        assert model is not None

    def test_explicit_api_key_raises_instead_of_bypassing_oauth(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        """Enabled subscription auth must never be bypassed by an explicit key."""
        monkeypatch.setenv(ENV_TOGGLE, "1")
        for key_name in ("api_key", "openai_api_key"):
            with pytest.raises(RuntimeError, match="never be bypassed") as excinfo:
                subscription_model(_OPENAI_ID, {key_name: "sk-explicit"})
            assert key_name in str(excinfo.value)
            assert "sk-explicit" not in str(excinfo.value)

    def test_caller_include_list_is_not_mutated(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        """The aliased include list feeds cache keys; it must never grow in place."""
        monkeypatch.setenv(ENV_TOGGLE, "1")
        include = ["web_search_call.action.sources"]
        model = subscription_model(_OPENAI_ID, {"include": include, "max_tokens": 10})
        assert model is not None
        assert include == ["web_search_call.action.sources"]

    def test_max_tokens_is_stripped_for_codex_backend(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        """Codex rejects max_output_tokens; the caller's max_tokens must not bind."""
        monkeypatch.setenv(ENV_TOGGLE, "1")
        model = subscription_model(_OPENAI_ID, {"max_tokens": 4096})
        assert model is not None
        assert model.max_tokens is None

    def test_reasoning_defaults_when_caller_omits_it(
        self, monkeypatch: pytest.MonkeyPatch, chatgpt_store: Path
    ) -> None:
        """Codex masks a missing reasoning field as an overloaded error; default it."""
        monkeypatch.setenv(ENV_TOGGLE, "1")
        defaulted = subscription_model(_OPENAI_ID, {})
        explicit = subscription_model(_OPENAI_ID, {"reasoning": {"effort": "high"}})
        assert defaulted is not None and explicit is not None
        assert defaulted.reasoning == {"effort": "medium", "summary": "auto"}
        assert explicit.reasoning == {"effort": "high"}


class TestFailOpenFallthrough:
    """AC: missing store or unsupported provider falls through with one warning."""

    def test_missing_store_falls_through_to_api_key_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        captured_init: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(
            subscription_auth, "_chatgpt_store_path", lambda: tmp_path / "missing.json"
        )
        with caplog.at_level(logging.WARNING, logger=subscription_auth.__name__):
            make_model(_OPENAI_ID, max_tokens=1)
            model_module._MODEL_CACHE.clear()
            make_model(_OPENAI_ID, max_tokens=2)
        assert captured_init["kwargs"]["max_tokens"] == 2
        warnings = [r for r in caplog.records if "no ChatGPT OAuth token store" in r.message]
        assert len(warnings) == 1

    def test_provider_without_branch_returns_none_with_one_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(ENV_TOGGLE, "1")
        with caplog.at_level(logging.WARNING, logger=subscription_auth.__name__):
            assert subscription_model("google_genai:gemini-3-pro", {}) is None
            assert subscription_model("google_genai:gemini-3-pro", {}) is None
            assert subscription_model("fireworks:some-model", {}) is None
        google = [r for r in caplog.records if "'google_genai'" in r.message]
        fireworks = [r for r in caplog.records if "'fireworks'" in r.message]
        assert len(google) == 1
        assert len(fireworks) == 1


def _write_claude_store(path: Path, *, expires_in_seconds: float) -> dict[str, Any]:
    """Write a Claude Code-shaped credential file and return its creds dict."""
    creds = {
        "accessToken": "access-old",
        "refreshToken": "refresh-old",
        "expiresAt": int((time.time() + expires_in_seconds) * 1000),
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }
    path.write_text(json.dumps({"claudeAiOauth": creds}))
    return creds


class _StubTokenProvider:
    """Constant-token provider for payload tests; no store, no refresh."""

    def get_token(self) -> str:
        return "tok-123"

    async def aget_token(self) -> str:
        return "tok-123"


class TestClaudeTokenProvider:
    """AC: expiry-aware refresh with atomic rotated-pair write-back."""

    def _provider(self, tmp_path: Path, *, expires_in_seconds: float) -> ClaudeCodeTokenProvider:
        store = tmp_path / "credentials.json"
        _write_claude_store(store, expires_in_seconds=expires_in_seconds)
        return ClaudeCodeTokenProvider(path=store, use_keychain=False)

    def _keychain_write_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[ClaudeCodeTokenProvider, dict[str, Any], list[list[str]]]:
        store = tmp_path / "credentials.json"
        stale = _write_claude_store(store, expires_in_seconds=-10)
        keychain_payload = json.dumps({"claudeAiOauth": stale}).encode()
        commands: list[list[str]] = []

        def fake_security(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            if command[1] == "find-generic-password":
                return subprocess.CompletedProcess(command, 0, stdout=keychain_payload, stderr=b"")
            raise subprocess.CalledProcessError(
                1, command, stderr=b"write failed for access-new refresh-new"
            )

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                    "expires_in": 3600,
                }

        import httpx

        monkeypatch.setattr(subprocess, "run", fake_security)
        monkeypatch.setattr(httpx, "post", lambda *_, **__: _Response())
        return ClaudeCodeTokenProvider(path=store, use_keychain=True), stale, commands

    def test_fresh_token_returned_without_refresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        provider = self._provider(tmp_path, expires_in_seconds=3600)

        def _no_refresh(*args: object, **kwargs: object) -> None:
            raise AssertionError("refresh must not run for a fresh token")

        monkeypatch.setattr(ClaudeCodeTokenProvider, "_refresh", _no_refresh)
        assert provider.get_token() == "access-old"

    def test_expired_token_refreshes_and_writes_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        provider = self._provider(tmp_path, expires_in_seconds=-10)
        posted: dict[str, Any] = {}

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                    "expires_in": 3600,
                }

        def fake_post(url: str, *, data: dict[str, str], timeout: float) -> _Response:
            posted["url"] = url
            posted["data"] = data
            return _Response()

        import httpx

        monkeypatch.setattr(httpx, "post", fake_post)
        assert provider.get_token() == "access-new"
        assert posted["data"]["grant_type"] == "refresh_token"
        assert posted["data"]["refresh_token"] == "refresh-old"
        stored = json.loads(provider.path.read_text())["claudeAiOauth"]
        assert stored["accessToken"] == "access-new"
        assert stored["refreshToken"] == "refresh-new"
        assert stored["expiresAt"] > time.time() * 1000
        assert stored["subscriptionType"] == "max"  # untouched fields survive
        assert provider.path.stat().st_mode & 0o777 == 0o600

    def test_unreadable_store_raises(self, tmp_path: Path) -> None:
        provider = ClaudeCodeTokenProvider(path=tmp_path / "missing.json", use_keychain=False)
        with pytest.raises(FileNotFoundError):
            provider.read()

    def test_keychain_write_failure_prefers_rotated_file_in_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider, _, commands = self._keychain_write_failure(monkeypatch, tmp_path)

        with caplog.at_level(logging.WARNING, logger=claude_code_model.__name__):
            assert provider.get_token() == "access-new"

        current, current_source = provider.read()
        restarted, restarted_source = ClaudeCodeTokenProvider(
            path=provider.path, use_keychain=True
        ).read()
        assert current["refreshToken"] == restarted["refreshToken"] == "refresh-new"
        assert current_source == restarted_source == "keychain-write-fallback"
        assert sum(command[1] == "find-generic-password" for command in commands) == 2
        wrapper = json.loads(provider.path.read_text())
        assert wrapper["speedbayCredentialSource"] == "keychain-write-fallback"
        assert provider.path.stat().st_mode & 0o777 == 0o600
        for secret in ("access-old", "refresh-old", "access-new", "refresh-new"):
            assert secret not in caplog.text

    def test_keychain_write_failure_prefers_rotated_file_after_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_run = subprocess.run
        provider, stale, _ = self._keychain_write_failure(monkeypatch, tmp_path)
        assert provider.get_token() == "access-new"

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        security = bin_dir / "security"
        keychain_payload = json.dumps({"claudeAiOauth": stale})
        security.write_text(
            f"#!/usr/bin/env python3\nimport sys\nsys.stdout.write({keychain_payload!r})\n"
        )
        security.chmod(0o755)
        script = """
import json
import sys
from pathlib import Path
from agent.speedbay.claude_code_model import ClaudeCodeTokenProvider

creds, source = ClaudeCodeTokenProvider(path=Path(sys.argv[1]), use_keychain=True).read()
print(json.dumps([creds["accessToken"], creds["refreshToken"], source]))
"""
        result = real_run(
            [sys.executable, "-c", script, str(provider.path)],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[2],
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )
        assert json.loads(result.stdout) == [
            "access-new",
            "refresh-new",
            "keychain-write-fallback",
        ]

    def test_marked_fallback_survives_later_refresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        provider, _, commands = self._keychain_write_failure(monkeypatch, tmp_path)
        assert provider.get_token() == "access-new"
        wrapper = json.loads(provider.path.read_text())
        wrapper["claudeAiOauth"]["expiresAt"] = 0
        provider.path.write_text(json.dumps(wrapper))

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "access_token": "access-newest",
                    "refresh_token": "refresh-newest",
                    "expires_in": 3600,
                }

        import httpx

        monkeypatch.setattr(httpx, "post", lambda *_, **__: _Response())
        replacements: list[tuple[Path, Path]] = []
        events: list[str] = []
        real_flock = claude_code_model.fcntl.flock
        real_replace = os.replace

        def track_flock(fd: int, operation: int) -> None:
            events.append("lock" if operation == claude_code_model.fcntl.LOCK_EX else "unlock")
            real_flock(fd, operation)

        def track_replace(source: Path, destination: Path) -> None:
            events.append("replace")
            replacements.append((source, destination))
            real_replace(source, destination)

        monkeypatch.setattr(claude_code_model.fcntl, "flock", track_flock)
        monkeypatch.setattr(os, "replace", track_replace)
        assert provider.get_token() == "access-newest"
        stored = json.loads(provider.path.read_text())
        assert stored["speedbayCredentialSource"] == "keychain-write-fallback"
        assert stored["claudeAiOauth"]["refreshToken"] == "refresh-newest"
        assert provider.path.stat().st_mode & 0o777 == 0o600
        assert replacements == [(provider.path.with_suffix(".tmp"), provider.path)]
        assert events == ["lock", "replace", "unlock"]
        assert sum(command[1] == "find-generic-password" for command in commands) == 2

    @pytest.mark.parametrize(
        "wrapper",
        [
            {"speedbayCredentialSource": "keychain-write-fallback", "claudeAiOauth": {}},
            {
                "speedbayCredentialSource": "keychain-write-fallback",
                "claudeAiOauth": {
                    "accessToken": "file-access",
                    "refreshToken": "file-refresh",
                    "expiresAt": "invalid",
                },
            },
            {"claudeAiOauth": {"accessToken": "unmarked", "refreshToken": "unmarked"}},
        ],
    )
    def test_invalid_or_unmarked_fallback_does_not_override_keychain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrapper: dict[str, Any]
    ) -> None:
        store = tmp_path / "credentials.json"
        store.write_text(json.dumps(wrapper))
        keychain = {
            "accessToken": "keychain-access",
            "refreshToken": "keychain-refresh",
            "expiresAt": int((time.time() + 3600) * 1000),
        }

        def fake_security(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
            payload = json.dumps({"claudeAiOauth": keychain}).encode()
            return subprocess.CompletedProcess(command, 0, stdout=payload, stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_security)
        creds, source = ClaudeCodeTokenProvider(path=store, use_keychain=True).read()
        assert creds == keychain
        assert source == "keychain"


class TestChatClaudeCode:
    """AC: payloads carry the Claude Code identity block and OAuth headers."""

    def _payload(self) -> dict[str, Any]:
        # Canonical field names resolve via populate_by_name at runtime.
        model = ChatClaudeCode(
            model="claude-opus-5",  # pyright: ignore[reportCallIssue]
            token_provider=_StubTokenProvider(),
            max_tokens=64,  # pyright: ignore[reportCallIssue]
        )
        return model._get_request_payload([SystemMessage("real system prompt"), HumanMessage("hi")])

    def test_identity_is_exact_first_system_block(self) -> None:
        payload = self._payload()
        system = payload["system"]
        assert system[0] == {"type": "text", "text": CLAUDE_CODE_IDENTITY}
        assert "real system prompt" in str(system[1])

    def test_bearer_auth_without_api_key_header(self) -> None:
        from anthropic import Omit

        headers = self._payload()["extra_headers"]
        # Exact SDK casing: anthropic merges header dicts case-sensitively and
        # emits auth as "X-Api-Key"; lowercase keys would strip nothing.
        assert headers["Authorization"] == "Bearer tok-123"
        assert isinstance(headers["X-Api-Key"], Omit)
        assert "authorization" not in headers
        assert "x-api-key" not in headers
        assert headers["user-agent"].startswith("claude-cli/")
        assert headers["x-app"] == "cli"

    def test_beta_header_carries_both_oauth_flags(self) -> None:
        beta = self._payload()["extra_headers"]["anthropic-beta"]
        assert "claude-code-20250219" in beta
        assert "oauth-2025-04-20" in beta


class TestAnthropicBranch:
    """AC: make_model routes anthropic ids through ChatClaudeCode, fail-open."""

    def test_make_model_returns_chat_claude_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = tmp_path / "credentials.json"
        _write_claude_store(store, expires_in_seconds=3600)
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(claude_code_model, "_default_credentials_path", lambda: store)
        monkeypatch.setattr(claude_code_model, "_default_use_keychain", lambda: False)
        model = make_model("anthropic:claude-opus-5", max_tokens=64)
        assert isinstance(model, ChatClaudeCode)
        assert model.token_provider.path == store

    def test_explicit_api_key_raises_instead_of_bypassing_oauth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Enabled subscription auth must never be bypassed by an explicit key."""
        store = tmp_path / "credentials.json"
        _write_claude_store(store, expires_in_seconds=3600)
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(claude_code_model, "_default_credentials_path", lambda: store)
        monkeypatch.setattr(claude_code_model, "_default_use_keychain", lambda: False)
        for key_name in ("api_key", "anthropic_api_key"):
            with pytest.raises(RuntimeError, match="never be bypassed") as excinfo:
                subscription_model("anthropic:claude-opus-5", {key_name: "sk-ant-explicit"})
            assert key_name in str(excinfo.value)
            assert "sk-ant-explicit" not in str(excinfo.value)

    def test_environment_api_key_does_not_bypass_subscription_oauth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = tmp_path / "credentials.json"
        _write_claude_store(store, expires_in_seconds=3600)
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-only-key")
        monkeypatch.setattr(claude_code_model, "_default_credentials_path", lambda: store)
        monkeypatch.setattr(claude_code_model, "_default_use_keychain", lambda: False)
        model = make_model("anthropic:claude-opus-5", max_tokens=64)
        assert isinstance(model, ChatClaudeCode)

    def test_unreadable_store_falls_through_to_api_key_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        captured_init: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(
            claude_code_model, "_default_credentials_path", lambda: tmp_path / "missing.json"
        )
        monkeypatch.setattr(claude_code_model, "_default_use_keychain", lambda: False)
        with caplog.at_level(logging.WARNING, logger=subscription_auth.__name__):
            make_model("anthropic:claude-opus-5", max_tokens=64)
        assert captured_init["model"] == "anthropic:claude-opus-5"
        warnings = [r for r in caplog.records if "Claude Code credential store" in r.message]
        assert len(warnings) == 1

    async def test_in_loop_construction_skips_the_blocking_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """langgraph dev forbids blocking reads in the loop; construct optimistically.

        asyncio_mode=auto runs this test inside a running event loop, matching
        the async graph-factory context where the probe previously exploded
        into a silent API-key fallback (OPE-67).
        """

        class _MustNotProbe(ClaudeCodeTokenProvider):
            def read(self):  # noqa: ANN202
                raise AssertionError("the blocking probe must not run inside a loop")

        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(claude_code_model, "ClaudeCodeTokenProvider", _MustNotProbe)
        model = subscription_model("anthropic:claude-opus-5", {})
        assert isinstance(model, ChatClaudeCode)

    def test_empty_store_falls_through_to_api_key_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        captured_init: dict[str, Any],
    ) -> None:
        """A parseable store without the token fields must not construct a model."""
        store = tmp_path / "credentials.json"
        store.write_text(json.dumps({"claudeAiOauth": {}}))
        monkeypatch.setenv(ENV_TOGGLE, "1")
        monkeypatch.setattr(claude_code_model, "_default_credentials_path", lambda: store)
        monkeypatch.setattr(claude_code_model, "_default_use_keychain", lambda: False)
        make_model("anthropic:claude-opus-5", max_tokens=64)
        assert captured_init["model"] == "anthropic:claude-opus-5"
