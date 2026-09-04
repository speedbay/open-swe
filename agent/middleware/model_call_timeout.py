"""Wall-clock deadline around every model call.

Provider ``timeout`` kwargs bound HTTP requests, but the OpenAI Responses
websocket transport can stall without the client ever raising a read timeout,
which parks the whole run: no stream events, no queued-message pickup, and
nothing for ``ModelFallbackMiddleware`` to react to (a hang is not an error).
This middleware turns that hang into a timeout error, so the fallback model — or
the run-completion webhook — reports it instead of the run going silent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

logger = logging.getLogger(__name__)

# SPEEDBAY DEVIATION (OPE-125): Detached cancellation-resistant calls must not
# extend the model-call wall-clock deadline; see FORK.md.
_detached_model_call_tasks: set[asyncio.Task[ModelResponse]] = set()

# Above the provider-level ``timeout`` (agent.utils.model), so a stalled HTTP
# request fails and retries inside the provider client first and this only fires
# for stalls the provider never notices.
DEFAULT_MODEL_CALL_TIMEOUT_SECONDS = 900.0


class ModelCallTimeoutError(TimeoutError):
    """A model call exceeded its wall-clock deadline."""


def _configured_timeout_seconds() -> float:
    raw = os.environ.get("OPEN_SWE_MODEL_CALL_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MODEL_CALL_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_MODEL_CALL_TIMEOUT_SECONDS


class ModelCallTimeoutMiddleware(AgentMiddleware):
    """Fail a model call that exceeds the deadline instead of hanging forever."""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        super().__init__()
        self._timeout_seconds = timeout_seconds or _configured_timeout_seconds()

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        task = asyncio.create_task(handler(request))
        try:
            done, _ = await asyncio.wait({task}, timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            self._detach_cancelled_task(task)
            raise
        if done:
            return task.result()

        self._detach_cancelled_task(task)
        logger.warning("Model call exceeded %ss deadline; aborting", self._timeout_seconds)
        raise ModelCallTimeoutError(f"Model call exceeded the {self._timeout_seconds}s deadline")

    @staticmethod
    def _detach_cancelled_task(task: asyncio.Task[ModelResponse]) -> None:
        task.cancel()
        _detached_model_call_tasks.add(task)

        def _observe_detached_task(completed_task: asyncio.Task[ModelResponse]) -> None:
            _detached_model_call_tasks.discard(completed_task)
            if not completed_task.cancelled():
                completed_task.exception()

        task.add_done_callback(_observe_detached_task)
