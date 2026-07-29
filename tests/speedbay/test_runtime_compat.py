"""Tests for the server-runtime config strip (OPE-15).

Run:  .venv/bin/python -m pytest tests/speedbay/test_runtime_compat.py -x -q
"""

from __future__ import annotations

from agent.speedbay.runtime_compat import strip_server_runtime


def test_strips_only_the_pregel_runtime_key_without_mutating_input() -> None:
    sentinel = object()
    config = {
        "recursion_limit": 25,
        "configurable": {"__pregel_runtime": sentinel, "thread_id": "t-1"},
    }
    stripped = strip_server_runtime(config)  # type: ignore[arg-type]
    assert stripped.get("configurable") == {"thread_id": "t-1"}
    assert stripped.get("recursion_limit") == 25
    # input untouched — the server still owns its copy
    assert config["configurable"]["__pregel_runtime"] is sentinel


def test_noop_returns_same_object_when_key_absent() -> None:
    config = {"configurable": {"thread_id": "t-1"}}
    assert strip_server_runtime(config) is config  # type: ignore[arg-type]
    empty = {}
    assert strip_server_runtime(empty) is empty  # type: ignore[arg-type]
