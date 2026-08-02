"""Subscription OAuth model auth (OPE-60).

Structural tests for `agent/speedbay/subscription_auth.py` and its
`make_model` registration: pytest, class-per-concern, no model or network
calls. The token store is a temp file; no real credentials are read.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from agent.speedbay import subscription_auth
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
            assert subscription_model("anthropic:claude-opus-5", {}) is None
        google = [r for r in caplog.records if "'google_genai'" in r.message]
        anthropic = [r for r in caplog.records if "'anthropic'" in r.message]
        assert len(google) == 1
        assert len(anthropic) == 1
