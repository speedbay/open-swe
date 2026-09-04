import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.runtime import Runtime

from agent import reviewer, server
from agent.utils.sandbox_state import SandboxUnreachableError


def _agent_middleware(thread_id: str) -> server.PrepareAgentRunMiddleware:
    return server.PrepareAgentRunMiddleware(
        thread_id=thread_id,
        config={"configurable": {}},
        profile_login=None,
        model_id="test-model",
        effort=None,
        source="test",
        user_email="",
        linear_project_id="",
        linear_issue_number="",
        create_prs=False,
        plan_mode=False,
        corridor_enabled=False,
    )


def _reviewer_middleware(thread_id: str) -> reviewer.PrepareReviewerRunMiddleware:
    return reviewer.PrepareReviewerRunMiddleware(
        thread_id=thread_id,
        config={"configurable": {}},
        use_gateway=False,
    )


async def _run_prepare(middleware: Any) -> None:
    await middleware.abefore_agent({"messages": []}, MagicMock(spec=Runtime))


def _agent_setup(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(server, "resolve_github_token", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(server, "_resolve_prompt_default_repo", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "resolve_triggering_user_identity", MagicMock(return_value=None))
    monkeypatch.setattr(server, "ensure_sandbox_for_thread", AsyncMock(side_effect=error))


async def _assert_failure(
    middleware: Any, error: SandboxUnreachableError, caplog: pytest.LogCaptureFixture, source: str
) -> None:
    with pytest.raises(SandboxUnreachableError) as excinfo:
        await _run_prepare(middleware)
    assert excinfo.value is error
    assert any(
        f"source={source} thread_id={error.thread_id} sandbox_id={error.sandbox_id}" in m
        for m in caplog.messages
    )


@pytest.mark.asyncio
async def test_agent_notification_failure_preserves_sandbox_unreachable_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = SandboxUnreachableError("agent-thread", "agent-sandbox", "unreachable")
    _agent_setup(monkeypatch, error)
    monkeypatch.setattr(
        server,
        "post_sandbox_unreachable_notification",
        AsyncMock(side_effect=RuntimeError("delivery failed")),
    )
    caplog.set_level(logging.ERROR, logger=server.logger.name)
    await _assert_failure(_agent_middleware(error.thread_id), error, caplog, "agent")
    assert "delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_reviewer_notification_failure_preserves_sandbox_unreachable_error(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = SandboxUnreachableError("reviewer-thread", "reviewer-sandbox", "unreachable")
    monkeypatch.setattr(
        reviewer, "_ensure_reviewer_sandbox_for_thread", AsyncMock(side_effect=error)
    )
    monkeypatch.setattr(
        reviewer,
        "post_sandbox_unreachable_notification",
        AsyncMock(side_effect=RuntimeError("delivery failed")),
    )
    caplog.set_level(logging.ERROR, logger=reviewer.logger.name)
    await _assert_failure(_reviewer_middleware(error.thread_id), error, caplog, "reviewer")
    assert "delivery failed" in caplog.text


async def _assert_awaited(
    monkeypatch: pytest.MonkeyPatch, module: Any, middleware: Any, error: SandboxUnreachableError
) -> None:
    completed = False

    async def notify(*_: object, **__: object) -> None:
        nonlocal completed
        await asyncio.sleep(0)
        completed = True

    notify_mock = AsyncMock(side_effect=notify)
    monkeypatch.setattr(module, "post_sandbox_unreachable_notification", notify_mock)
    with pytest.raises(SandboxUnreachableError) as excinfo:
        await _run_prepare(middleware)
    assert completed and excinfo.value is error
    if module is reviewer:
        assert notify_mock.await_args is not None
        assert notify_mock.await_args.kwargs["replacement_attempted"] is True


@pytest.mark.asyncio
async def test_agent_successful_notification_is_awaited_before_sandbox_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SandboxUnreachableError("agent-thread", "agent-sandbox", "unreachable")
    _agent_setup(monkeypatch, error)
    await _assert_awaited(monkeypatch, server, _agent_middleware(error.thread_id), error)


@pytest.mark.asyncio
async def test_reviewer_successful_notification_is_awaited_before_sandbox_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SandboxUnreachableError("reviewer-thread", "reviewer-sandbox", "unreachable")
    monkeypatch.setattr(
        reviewer, "_ensure_reviewer_sandbox_for_thread", AsyncMock(side_effect=error)
    )
    await _assert_awaited(monkeypatch, reviewer, _reviewer_middleware(error.thread_id), error)
