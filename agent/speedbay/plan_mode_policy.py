"""Speed Bay policy for restricting plan mode to explicit dispatches."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool

_GUIDANCE_SECTION = re.compile(
    r"---\n\n### Plan Mode\n\n.*?\nPlan-review link for this conversation:.*?"
    r"(?=---(?:\n|$)|$)",
    re.DOTALL,
)


def _tool_name(tool: BaseTool | dict[str, Any] | Any) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
    else:
        name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class PlanModePolicyMiddleware(AgentMiddleware):
    """Remove model-initiated plan mode from every model request."""

    @staticmethod
    def _apply(request: ModelRequest) -> ModelRequest:
        tools = [tool for tool in request.tools if _tool_name(tool) != "enter_plan_mode"]
        system_message = request.system_message
        stripped = (
            _GUIDANCE_SECTION.sub("", system_message.text) if system_message is not None else None
        )
        updates: dict[str, Any] = {}
        if len(tools) != len(request.tools):
            updates["tools"] = tools
        if system_message is not None and stripped != system_message.text:
            updates["system_message"] = SystemMessage(content=stripped)
        return request.override(**updates) if updates else request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._apply(request))
