from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import openai
import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from agent.middleware.model_call_timeout import ModelCallTimeoutError, ModelCallTimeoutMiddleware
from agent.middleware.model_fallback import ModelFallbackMiddleware


def _request() -> ModelRequest[None]:
    request = MagicMock()
    request.override = MagicMock(return_value=MagicMock(name="fallback_request"))
    return cast(ModelRequest[None], request)


async def _settle(release: asyncio.Event, settled: asyncio.Event) -> None:
    release.set()
    await asyncio.wait_for(settled.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_deadline_returns_when_handler_suppresses_cancellation() -> None:
    suppressed = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        except asyncio.CancelledError:
            suppressed.set()
            await release.wait()
            settled.set()
            return cast(ModelResponse[Any], MagicMock())

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(ModelCallTimeoutError):
            await ModelCallTimeoutMiddleware(timeout_seconds=0.02).awrap_model_call(
                _request(), handler
            )
        elapsed = loop.time() - started
        await asyncio.sleep(0)
        assert suppressed.is_set()
        assert elapsed < 0.1
    finally:
        await _settle(release, settled)


@pytest.mark.asyncio
async def test_late_orphan_exception_is_observed() -> None:
    release = asyncio.Event()
    settled = asyncio.Event()
    exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: exceptions.append(context))

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        except asyncio.CancelledError:
            await release.wait()
            settled.set()
            raise RuntimeError("late sentinel") from None

    try:
        with pytest.raises(ModelCallTimeoutError):
            await ModelCallTimeoutMiddleware(timeout_seconds=0.02).awrap_model_call(
                _request(), handler
            )
        await _settle(release, settled)
        assert not any(
            context.get("message") == "Task exception was never retrieved" for context in exceptions
        )
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_fallback_sequence_respects_one_total_elapsed_budget() -> None:
    suppressed = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    attempts = 0

    async def provider(_request: ModelRequest[None]) -> ModelResponse[Any]:
        nonlocal attempts
        attempts += 1
        try:
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            suppressed.set()
            await release.wait()
            settled.set()
            return cast(ModelResponse[Any], MagicMock())
        raise openai.APIConnectionError(request=MagicMock())

    fallback = ModelFallbackMiddleware(
        MagicMock(), backoff_schedule=(0.0, 0.0, 0.0, 0.0, 0.0), surface_outage_message=False
    )
    timeout = ModelCallTimeoutMiddleware(timeout_seconds=0.06)

    async def fallback_handler(request: ModelRequest[None]) -> ModelResponse[Any]:
        return await fallback.awrap_model_call(request, provider)

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(ModelCallTimeoutError):
            await timeout.awrap_model_call(_request(), fallback_handler)
        elapsed = loop.time() - started
        await asyncio.sleep(0)
        assert suppressed.is_set()
        assert elapsed < 0.15
    finally:
        await _settle(release, settled)
