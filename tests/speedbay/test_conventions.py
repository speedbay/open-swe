"""Unit tests for SpeedbayConventionsMiddleware.

SPEEDBAY ORG-LAYER FILE (see FORK.md). Mirrors the upstream ``tests/middleware``
conventions: pytest, class-per-concern, no model or network calls, structural
assertions (per the LangChain unit-testing guidance of exercising middleware in
isolation with in-memory objects).

The regression cases pin the documented ``ModelRequest`` contract:
``system_message`` is a ``SystemMessage | None``; formatting the object instead
of its ``.text`` serialized the message repr into the prompt, and checking
``in`` against the object silently defeated the idempotence marker.
"""

from typing import Any, cast

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage

from agent.speedbay.conventions import (
    SPEEDBAY_CONVENTIONS,
    SpeedbayConventionsMiddleware,
)

_MARKER = "Speed Bay repository conventions"


def _make_request(system_message: SystemMessage | None) -> ModelRequest[None]:
    request = ModelRequest(
        model=cast(Any, None),
        system_message=system_message,
        messages=[],
        tool_choice=None,
        tools=[],
        response_format=None,
    )
    return cast("ModelRequest[None]", request)


class TestAugment:
    def test_appends_conventions_preserving_existing_text(self) -> None:
        request = _make_request(SystemMessage(content="upstream prompt text"))

        result = SpeedbayConventionsMiddleware._augment(request)

        assert result.system_message is not None
        text = result.system_message.text
        assert text.startswith("upstream prompt text")
        assert _MARKER in text

    def test_none_system_message_yields_conventions_only(self) -> None:
        request = _make_request(None)

        result = SpeedbayConventionsMiddleware._augment(request)

        assert result.system_message is not None
        assert _MARKER in result.system_message.text

    def test_idempotent_across_repeated_calls(self) -> None:
        request = _make_request(SystemMessage(content="upstream prompt text"))

        once = SpeedbayConventionsMiddleware._augment(request)
        twice = SpeedbayConventionsMiddleware._augment(once)

        assert twice is once
        assert twice.system_message is not None
        assert twice.system_message.text.count(_MARKER) == 1

    def test_result_is_a_system_message_not_a_string(self) -> None:
        request = _make_request(SystemMessage(content="upstream prompt text"))

        result = SpeedbayConventionsMiddleware._augment(request)

        assert isinstance(result.system_message, SystemMessage)

    def test_message_repr_is_not_serialized_into_the_prompt(self) -> None:
        # Regression: f-string over the SystemMessage object leaked
        # "content='...' additional_kwargs={}" into the prompt text.
        request = _make_request(SystemMessage(content="upstream prompt text"))

        result = SpeedbayConventionsMiddleware._augment(request)

        assert result.system_message is not None
        assert "additional_kwargs" not in result.system_message.text

    def test_original_request_is_not_mutated(self) -> None:
        original_message = SystemMessage(content="upstream prompt text")
        request = _make_request(original_message)

        SpeedbayConventionsMiddleware._augment(request)

        assert request.system_message is original_message
        assert _MARKER not in original_message.text

    def test_marker_is_present_in_conventions_text(self) -> None:
        # The idempotence check greps for the marker; if the conventions text
        # is reworded without it, stacking returns silently.
        assert _MARKER in SPEEDBAY_CONVENTIONS


class TestSimplicityLadder:
    _HEADING = "## Implementation quality — the simplicity ladder"
    _RUNGS = (
        "Does this need to exist at all?",
        "Reuse a helper, utility, or pattern already in this codebase.",
        "Use the standard library",
        "Use a native platform or database feature",
        "Use an already-installed dependency",
        "Make it one line",
        "Only then write the minimum code that works.",
    )

    def test_augmented_prompt_contains_heading_and_ordered_rungs(self) -> None:
        seen: list[ModelRequest[None]] = []

        def handler(request: ModelRequest[None]) -> ModelResponse[Any]:
            seen.append(request)
            return cast("ModelResponse[Any]", {"messages": []})

        SpeedbayConventionsMiddleware().wrap_model_call(_make_request(None), cast(Any, handler))

        assert len(seen) == 1
        assert seen[0].system_message is not None
        text = seen[0].system_message.text
        assert self._HEADING in text
        positions = [text.index(rung) for rung in self._RUNGS]
        assert positions == sorted(positions)

    def test_contains_root_cause_rule(self) -> None:
        assert "Fix root causes, not symptoms." in SPEEDBAY_CONVENTIONS
        assert "find every caller" in SPEEDBAY_CONVENTIONS
        assert "sibling callers broken" in SPEEDBAY_CONVENTIONS

    def test_contains_ceiling_marker_and_carve_outs(self) -> None:
        assert "`# ponytail:`" in SPEEDBAY_CONVENTIONS
        assert "input validation at trust boundaries" in SPEEDBAY_CONVENTIONS
        assert "error handling that prevents data loss" in SPEEDBAY_CONVENTIONS
        assert "security measures" in SPEEDBAY_CONVENTIONS
        assert "Explicit ACs always win" in SPEEDBAY_CONVENTIONS


class TestWrapModelCall:
    def test_sync_handler_receives_augmented_request(self) -> None:
        middleware = SpeedbayConventionsMiddleware()
        request = _make_request(SystemMessage(content="upstream prompt text"))
        seen: list[ModelRequest[None]] = []

        def handler(req: ModelRequest[None]) -> ModelResponse[Any]:
            seen.append(req)
            return cast("ModelResponse[Any]", {"messages": []})

        middleware.wrap_model_call(request, cast(Any, handler))

        assert len(seen) == 1
        assert seen[0].system_message is not None
        assert _MARKER in seen[0].system_message.text

    async def test_async_handler_receives_augmented_request(self) -> None:
        middleware = SpeedbayConventionsMiddleware()
        request = _make_request(SystemMessage(content="upstream prompt text"))
        seen: list[ModelRequest[None]] = []

        async def handler(req: ModelRequest[None]) -> ModelResponse[Any]:
            seen.append(req)
            return cast("ModelResponse[Any]", {"messages": []})

        await middleware.awrap_model_call(request, cast(Any, handler))

        assert len(seen) == 1
        assert seen[0].system_message is not None
        assert _MARKER in seen[0].system_message.text


class TestBranchRule:
    """OPE-58: the conventions text must countermand upstream's branch guidance."""

    def test_names_the_team_nnn_slug_pattern(self) -> None:
        assert "`<team>-NNN-<slug>`" in SPEEDBAY_CONVENTIONS

    def test_prohibits_the_upstream_open_swe_prefix(self) -> None:
        assert "Never `open-swe/<slug>`" in SPEEDBAY_CONVENTIONS
        # Explicit override wording, since upstream's instruction appears
        # earlier in the assembled prompt and must lose to this one.
        assert "overrides" in SPEEDBAY_CONVENTIONS
