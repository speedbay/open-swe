"""Regressions for Speed Bay's model-initiated plan-mode policy."""

from typing import Any, cast

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool

from agent.prompt import PLAN_MODE_GUIDANCE_SECTION, PLAN_MODE_SECTION
from agent.speedbay.plan_mode_policy import PlanModePolicyMiddleware


@tool
def enter_plan_mode() -> None:
    """Enter plan mode."""


@tool
def approve_plan() -> None:
    """Approve plan mode."""


@tool
def save_plan() -> None:
    """Save a plan."""


def _request(text: str, *, plan_mode: bool = False) -> ModelRequest:
    return ModelRequest(
        model=cast(BaseChatModel, object()),
        messages=[],
        system_message=SystemMessage(content=text),
        tools=[enter_plan_mode, approve_plan, save_plan],
        state=cast(Any, {"plan_mode": plan_mode}),
    )


def _apply(request: ModelRequest) -> ModelRequest:
    return PlanModePolicyMiddleware._apply(request)


def test_enter_plan_mode_never_offered() -> None:
    for plan_mode in (False, True):
        result = _apply(_request("base", plan_mode=plan_mode))

        assert [tool.name for tool in result.tools] == ["approve_plan", "save_plan"]


def test_guidance_section_stripped() -> None:
    guidance = PLAN_MODE_GUIDANCE_SECTION.format(plan_review_url="https://example.test/plans/123")
    request = _request(f"before\n{guidance}---\n\nafter")

    once = _apply(request)
    twice = _apply(once)

    assert once.system_message is not None
    assert once.system_message.text == "before\n---\n\nafter"
    assert twice.system_message is once.system_message
    assert twice.system_message.text == once.system_message.text


def test_active_plan_mode_untouched() -> None:
    active = PLAN_MODE_SECTION.format(plan_url="https://example.test/plans/123")
    guidance = PLAN_MODE_GUIDANCE_SECTION.format(plan_review_url="https://example.test/plans/123")
    result = _apply(_request(f"prefix{guidance}{active}suffix", plan_mode=True))

    assert result.system_message is not None
    assert result.system_message.text == f"prefix{active}suffix"
    assert active in result.system_message.text
    assert [tool.name for tool in result.tools] == ["approve_plan", "save_plan"]


def test_absent_guidance_noop() -> None:
    text = "before\n\n### Plan Mode (ACTIVE)\n\nactive instructions\n\nafter"
    request = ModelRequest(
        model=cast(BaseChatModel, object()),
        messages=[],
        system_message=SystemMessage(content=text),
        tools=[approve_plan, save_plan],
    )

    result = _apply(request)

    assert result is request
    assert result.system_message is request.system_message
    assert result.system_message.text == text
