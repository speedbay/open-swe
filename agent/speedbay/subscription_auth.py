"""Subscription OAuth model auth (OPE-60, OPE-61).

SPEEDBAY ORG-LAYER FILE. Upstream does not own this module; its only contact
with upstream source is one marked block at the top of ``make_model`` in
``agent/utils/model.py`` (see FORK.md, "Re-check after every merge").

When ``SPEEDBAY_SUBSCRIPTION_AUTH`` is truthy, :func:`subscription_model`
builds chat models that authenticate with the team's ChatGPT subscription
OAuth tokens instead of metered API keys. Only a toggle that is off or a
provider without a subscription branch returns ``None`` (fall through to the
unchanged API-key path, ``init_chat_model``). For supported providers the
contract is fail-closed (OPE-175): a missing or unusable credential store
raises a redacted ``RuntimeError`` instead of silently selecting metered
API-key auth, and an explicit caller API key raises because it would bypass
subscription OAuth (OPE-144 review decision). Server graph factories may
represent the construction error as a ``DeferredErrorModel`` until first
invocation; that still never authenticates with a metered key.

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


def subscription_auth_enabled() -> bool:
    """Whether subscription auth is opted in via ``SPEEDBAY_SUBSCRIPTION_AUTH``."""
    return (os.environ.get(ENV_TOGGLE) or "").strip().lower() in {"1", "true", "yes", "on"}


def _chatgpt_store_path() -> Path:
    """ChatGPT OAuth token-store path (seam for tests; default ``~/.langchain/...``)."""
    from langchain_openai.chatgpt_oauth import DEFAULT_STORE_PATH

    return DEFAULT_STORE_PATH


def _reject_explicit_api_key(provider: str, model_kwargs: dict[str, object], *keys: str) -> None:
    """Raise when a caller-supplied API key would bypass subscription OAuth.

    Subscription auth, once enabled, must never be silently bypassed by an
    explicit credential (OPE-144 review decision). The key value is never
    included in the message.
    """
    supplied = [key for key in keys if model_kwargs.get(key) is not None]
    if supplied:
        raise RuntimeError(
            f"Subscription auth ({ENV_TOGGLE}) is enabled but an explicit {provider} "
            f"API key was supplied ({', '.join(supplied)}). Subscription OAuth must "
            f"never be bypassed; drop the explicit key or unset {ENV_TOGGLE}."
        )


def _openai_model(model_name: str, model_kwargs: dict[str, object]) -> Any | None:
    """Build a ``_ChatOpenAICodex`` for ``model_name``; fail closed otherwise.

    Raises on an explicit caller API key (see :func:`_reject_explicit_api_key`)
    and on a missing token store (OPE-175): falling through would construct a
    metered API-key model. Drops the kwargs ``_ChatOpenAICodex`` forces or pins (it raises on
    conflicting values) and replicates the two stateless-responses kwargs
    ``make_model`` would otherwise apply after this branch returns
    (``output_version`` and encrypted-reasoning ``include``).
    """
    _reject_explicit_api_key("OpenAI", model_kwargs, "api_key", "openai_api_key")
    store_path = _chatgpt_store_path()
    if not store_path.is_file():
        raise RuntimeError(
            f"Subscription auth ({ENV_TOGGLE}) is enabled but no ChatGPT OAuth token "
            f"store exists at {store_path}; refusing to fall back to metered OpenAI "
            "API-key auth. One-time setup: login_chatgpt_device() (see OPERATIONS.md "
            f"§ Subscription OAuth), or unset {ENV_TOGGLE}."
        )

    from langchain_openai.chat_models.codex import _ChatOpenAICodex
    from langchain_openai.chatgpt_oauth import _FileChatGPTOAuthTokenProvider

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
    """Build a ``ChatClaudeCode`` for ``model_name``; fail closed otherwise.

    Raises on an explicit caller API key (see :func:`_reject_explicit_api_key`)
    and, in loop-free contexts, on an unreadable or incomplete credential
    store (OPE-175): falling through would construct a metered API-key model.
    """
    _reject_explicit_api_key("Anthropic", model_kwargs, "api_key", "anthropic_api_key")

    import asyncio

    from .claude_code_model import ChatClaudeCode, ClaudeCodeTokenProvider

    provider = ClaudeCodeTokenProvider()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        in_loop = False
    else:
        in_loop = True
    if not in_loop:
        # Probe only when no event loop is running: the `security` subprocess
        # is a blocking os.read, and langgraph dev's blocking-call detector
        # raises on it inside async graph factories — which this except would
        # misread as "store unreadable" (observed live 2026-08-02, OPE-67).
        # ponytail: in-loop construction is optimistic; genuinely missing
        # credentials then fail per request (aget_token runs off-loop) through
        # the OAuth model itself — never through metered API-key auth.
        try:
            creds, _ = provider.read()
            missing = {"accessToken", "refreshToken"} - creds.keys()
            if missing:
                raise KeyError(f"credential store missing {sorted(missing)}")
        except Exception as exc:
            # Redacted: exception type only, never store contents (OPE-175).
            raise RuntimeError(
                f"Subscription auth ({ENV_TOGGLE}) is enabled but the Claude Code "
                f"credential store is unusable ({type(exc).__name__}); refusing to "
                "fall back to metered Anthropic API-key auth. One-time setup: run "
                "`claude` on this machine (see OPERATIONS.md § Subscription OAuth), "
                f"or unset {ENV_TOGGLE}."
            ) from exc
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

    ``None`` means "no subscription route" — the toggle is off or the provider
    has no subscription branch — and the caller (``make_model``) falls through
    to the API-key ``init_chat_model`` path unchanged. Supported providers are
    fail-closed (OPE-175): a missing or unusable credential store raises a
    redacted ``RuntimeError``, and an explicit caller API key raises instead
    of bypassing OAuth (:func:`_reject_explicit_api_key`).
    """
    if not subscription_auth_enabled():
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
