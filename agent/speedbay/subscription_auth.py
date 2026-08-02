"""Subscription OAuth model auth (OPE-60).

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

    kwargs: dict[str, Any] = dict(model_kwargs)
    for forced in ("base_url", "use_responses_api", "store", "streaming"):
        kwargs.pop(forced, None)
    kwargs.setdefault("output_version", "responses/v1")
    include = kwargs.get("include")
    if include is None:
        kwargs["include"] = ["reasoning.encrypted_content"]
    elif isinstance(include, list) and "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    return _ChatOpenAICodex(
        model=model_name,
        token_provider=_FileChatGPTOAuthTokenProvider(path=store_path),
        **kwargs,
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
    _warn_once(
        f"provider-{provider}",
        "Subscription auth enabled but provider %r has no subscription branch; "
        "using API-key auth for %s.",
        provider,
        model_id,
    )
    return None
