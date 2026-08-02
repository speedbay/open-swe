"""Subscription OAuth model auth (OPE-60, OPE-61).

SPEEDBAY ORG-LAYER FILE. Upstream does not own this module; its only contact
with upstream source is one marked block at the top of ``make_model`` in
``agent/utils/model.py`` (see FORK.md, "Re-check after every merge").

When ``SPEEDBAY_SUBSCRIPTION_AUTH`` is truthy, :func:`subscription_model`
builds chat models that authenticate with the team's ChatGPT subscription
OAuth tokens instead of metered API keys. On any problem — toggle off,
provider without a subscription branch, credentials unreadable — it returns
``None`` with a warn-once log and callers fall through to the unchanged
API-key path (``init_chat_model``), mirroring the fail-open contract of
:func:`agent.utils.gateway.gateway_overrides`.

The OpenAI branch is configuration only: langchain-openai ships
``_ChatOpenAICodex`` (ChatGPT codex backend, forced ``store=False`` /
``streaming=True`` / responses API, SystemMessage-to-``instructions`` lift,
refresh-aware per-request auth headers) plus the ``chatgpt_oauth``
login/refresh/token-store module. One-time operator login:
``login_chatgpt_device()`` — see OPERATIONS.md § Subscription OAuth.

The Anthropic branch (OPE-61) authenticates with Claude Code's own OAuth
credentials via :class:`agent.speedbay.claude_code_model.ChatClaudeCode`;
setup is Claude Code login itself (run ``claude`` once on this machine).

Both this and the upstream class are unofficial subscription-token paths;
``_ChatOpenAICodex`` warns "experimental and unofficial" at construction.
The env toggle keeps API-key billing the default (decision recorded in
OPE-60).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_TOGGLE = "SPEEDBAY_SUBSCRIPTION_AUTH"

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """Log ``message`` once per process per ``key`` (mirrors gateway logging)."""
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


def _enabled() -> bool:
    """Whether subscription auth is opted in via ``SPEEDBAY_SUBSCRIPTION_AUTH``."""
    return (os.environ.get(ENV_TOGGLE) or "").strip().lower() in {"1", "true", "yes", "on"}


def _chatgpt_store_path() -> Path:
    """ChatGPT OAuth token-store path (seam for tests; default ``~/.langchain/...``)."""
    from langchain_openai.chatgpt_oauth import DEFAULT_STORE_PATH

    return DEFAULT_STORE_PATH


def _openai_model(model_name: str, model_kwargs: dict[str, object]) -> Any | None:
    """Build a ``_ChatOpenAICodex`` for ``model_name``, or ``None`` to fall through.

    Drops the kwargs ``_ChatOpenAICodex`` forces or pins (it raises on
    conflicting values) and replicates the two stateless-responses kwargs
    ``make_model`` would otherwise apply after this branch returns
    (``output_version`` and encrypted-reasoning ``include``).
    """
    store_path = _chatgpt_store_path()
    if not store_path.is_file():
        _warn_once(
            "openai-credentials",
            "Subscription auth enabled but no ChatGPT OAuth token store at %s; "
            "falling back to API-key auth. One-time setup: login_chatgpt_device() "
            "(see OPERATIONS.md).",
            store_path,
        )
        return None

    from langchain_openai.chat_models.codex import _ChatOpenAICodex
    from langchain_openai.chatgpt_oauth import _FileChatGPTOAuthTokenProvider

    if model_kwargs.get("api_key") is not None or model_kwargs.get("openai_api_key") is not None:
        # An explicit caller credential means API-key auth was requested;
        # honor it via the fall-through instead of stripping it (the OAuth
        # model rejects api_key kwargs as conflicting).
        _warn_once(
            "openai-explicit-api-key",
            "Subscription auth enabled but an explicit api_key was supplied; "
            "using the API-key path for this model.",
        )
        return None

    kwargs: dict[str, Any] = dict(model_kwargs)
    # max_tokens: the codex backend rejects the mapped max_output_tokens field
    # (400 "Unsupported parameter", observed live 2026-08-02); OPE-60 recorded
    # stripping it as the decided fallback. The backend bounds output itself.
    for forced in ("base_url", "use_responses_api", "store", "streaming", "max_tokens"):
        kwargs.pop(forced, None)
    kwargs.setdefault("output_version", "responses/v1")
    # The codex backend masks a missing `reasoning` field as "Our servers are
    # currently overloaded" (observed live 2026-08-02). Every production caller
    # sends one via openai_reasoning_for; default defensively for the rest.
    # Literal mirrors DEFAULT_LLM_REASONING (importing it would be circular:
    # agent.utils.model imports this module).
    kwargs.setdefault("reasoning", {"effort": "medium", "summary": "auto"})
    include = kwargs.get("include")
    if include is None:
        kwargs["include"] = ["reasoning.encrypted_content"]
    elif isinstance(include, list) and "reasoning.encrypted_content" not in include:
        # Copy, never append in place: the list object is aliased from the
        # caller's kwargs (shallow dict copy) and feeds cache keys.
        kwargs["include"] = [*include, "reasoning.encrypted_content"]
    return _ChatOpenAICodex(
        model=model_name,
        token_provider=_FileChatGPTOAuthTokenProvider(path=store_path),
        **kwargs,
    )


def _anthropic_model(model_name: str, model_kwargs: dict[str, object]) -> Any | None:
    """Build a ``ChatClaudeCode`` for ``model_name``, or ``None`` to fall through.

    The credential store is probed up front so an unauthenticated machine
    falls back to API-key auth at construction time instead of failing every
    model call at request time.
    """
    from .claude_code_model import ChatClaudeCode, ClaudeCodeTokenProvider

    provider = ClaudeCodeTokenProvider()
    try:
        creds, _ = provider.read()
        missing = {"accessToken", "refreshToken"} - creds.keys()
        if missing:
            raise KeyError(f"credential store missing {sorted(missing)}")
    except Exception as exc:
        _warn_once(
            "anthropic-credentials",
            "Subscription auth enabled but the Claude Code credential store is "
            "unreadable (%s); falling back to API-key auth. One-time setup: run "
            "`claude` on this machine.",
            exc,
        )
        return None
    # pydantic synthesizes __init__ from field aliases (model_name), but
    # populate_by_name accepts the canonical names at runtime — the same call
    # shape init_chat_model uses.
    return ChatClaudeCode(
        model=model_name,  # pyright: ignore[reportCallIssue]
        token_provider=provider,
        **dict(model_kwargs),
    )


def subscription_model(model_id: str, model_kwargs: dict[str, object]) -> Any | None:
    """Return a subscription-OAuth chat model for ``model_id``, or ``None``.

    ``None`` means "no subscription route": the caller (``make_model``) falls
    through to the API-key ``init_chat_model`` path unchanged. Never raises
    for a missing or unreadable credential store — fail-open, log-don't-raise.
    """
    if not _enabled():
        return None
    provider, _, model_name = model_id.partition(":")
    if provider == "openai":
        return _openai_model(model_name, model_kwargs)
    if provider == "anthropic":
        return _anthropic_model(model_name, model_kwargs)
    _warn_once(
        f"provider-{provider}",
        "Subscription auth enabled but provider %r has no subscription branch; "
        "using API-key auth for %s.",
        provider,
        model_id,
    )
    return None
