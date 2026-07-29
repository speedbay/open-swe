"""Tests for the per-project quality-gate middleware (OPE-9).

Unit tests drive the middleware and gate runner through a fake backend so no
sandbox is needed; one docker-gated integration test at the bottom proves the
execute path against a real container and self-skips where docker or the
sandbox image is unavailable.

Run:  .venv/bin/python -m pytest tests/speedbay/test_quality_gates.py -x -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from attrs import frozen
from langchain_core.messages import ToolMessage

from agent.speedbay import quality_gates as qg
from agent.speedbay.quality_gates import (
    GateCommand,
    QualityGatesMiddleware,
    run_quality_gates,
    touched_projects,
)


@frozen
class FakeResponse:
    output: str = ""
    exit_code: int | None = 0


class FakeBackend:
    """Sandbox-execute stub: substring-matched scripted responses, ordered log."""

    def __init__(self, script: dict[str, FakeResponse] | None = None) -> None:
        self.script = script or {}
        self.commands: list[str] = []

    async def aexecute(self, command: str, *, timeout: int | None = None) -> FakeResponse:
        self.commands.append(command)
        for needle, response in self.script.items():
            if needle in command:
                return response
        return FakeResponse()


def _run(coro):
    return asyncio.run(coro)


def _request(tool: str = "open_pull_request", base: str = "main", head: str = "") -> Any:
    """Duck-typed ToolCallRequest double (typed Any for the middleware calls)."""

    args: dict[str, str] = {"base": base}
    if head:
        args["head"] = head

    class Request:
        tool_call = {"name": tool, "args": args, "id": "call-1"}

    return Request()


@pytest.fixture()
def demo_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the ported command map with a tiny deterministic one."""
    monkeypatch.setattr(
        qg,
        "PROJECT_QUALITY_GATES",
        {
            "demo": (
                GateCommand("demo lint", "run-lint"),
                GateCommand("demo scoped", "run-scoped", paths=("app/**", "config.ts")),
            ),
            "other": (GateCommand("other test", "run-other"),),
        },
    )


# --- pure selection ---------------------------------------------------------


def test_touched_projects_maps_paths_to_configured_roots(demo_gates) -> None:
    result = touched_projects(["demo/app/x.py", "demo/README.md", "unknown/y.py", "rootfile"])
    assert result == {"demo": ["app/x.py", "README.md"]}


def test_every_configured_project_gate_fires() -> None:
    """Each real project's every gate dispatches when its paths are touched.

    Guards the real ``PROJECT_QUALITY_GATES`` map (the other tests swap in a
    demo map): selection indexes only configured roots — no KeyError is
    possible for unknown projects — and every configured gate, including
    path-filtered ones, is actually issued to the sandbox.
    """
    for project, gates in qg.PROJECT_QUALITY_GATES.items():
        backend = FakeBackend()
        # One generic path plus one match per path-filtered gate.
        paths = [f"{project}/x"] + [
            f"{project}/{gate.paths[0].replace('**', 'x')}" for gate in gates if gate.paths
        ]
        assert set(touched_projects(paths)) == {project}
        assert _run(run_quality_gates(backend, paths)) is None
        for gate in gates:
            assert gate.name and gate.command
            assert any(gate.command in issued for issued in backend.commands), (
                project,
                gate.name,
            )


# --- gate runner ------------------------------------------------------------


def test_commands_run_from_project_root_and_pass(demo_gates) -> None:
    backend = FakeBackend()
    failure = _run(run_quality_gates(backend, ["demo/app/main.py"]))
    assert failure is None
    assert backend.commands == [
        "cd /workspace/demo && set -o pipefail && ( run-lint ) 2>&1 | tail -c 2000",
        "cd /workspace/demo && set -o pipefail && ( run-scoped ) 2>&1 | tail -c 2000",
    ]


def test_path_filtered_gate_skipped_when_no_match(demo_gates) -> None:
    backend = FakeBackend()
    failure = _run(run_quality_gates(backend, ["demo/docs/readme.md"]))
    assert failure is None
    assert backend.commands == [
        "cd /workspace/demo && set -o pipefail && ( run-lint ) 2>&1 | tail -c 2000"
    ]


def test_failing_command_blocks_with_name_and_bounded_tail(demo_gates) -> None:
    long_output = "x" * 5000 + "TAIL-MARKER"
    backend = FakeBackend({"run-lint": FakeResponse(output=long_output, exit_code=1)})
    failure = _run(run_quality_gates(backend, ["demo/app/main.py"]))
    assert failure is not None
    assert failure.command_name == "demo lint"
    assert failure.kind == "failure"
    assert failure.output_tail.endswith("TAIL-MARKER")
    assert len(failure.output_tail) == qg._OUTPUT_TAIL_CHARS
    # first failure stops the run — the scoped command never executed
    assert len(backend.commands) == 1 and "run-lint" in backend.commands[0]
    # the true tail is captured command-side, before backend head-truncation
    assert backend.commands[0].endswith(f"| tail -c {qg._OUTPUT_TAIL_CHARS}")


def test_precondition_failure_distinguished_by_exit_127(demo_gates) -> None:
    backend = FakeBackend(
        {"run-lint": FakeResponse(output="sh: run-lint: not found", exit_code=127)}
    )
    failure = _run(run_quality_gates(backend, ["demo/a.py"]))
    assert failure is not None and failure.kind == "precondition"


def test_precondition_failure_distinguished_by_marker(demo_gates) -> None:
    backend = FakeBackend({"run-lint": FakeResponse(output="bash: uv: not found", exit_code=1)})
    failure = _run(run_quality_gates(backend, ["demo/a.py"]))
    assert failure is not None and failure.kind == "precondition"


def test_timeout_exit_code_classified(demo_gates) -> None:
    backend = FakeBackend({"run-lint": FakeResponse(output="", exit_code=124)})
    failure = _run(run_quality_gates(backend, ["demo/a.py"]))
    assert failure is not None and failure.kind == "timeout"


def test_unconfigured_project_passes_with_notice(demo_gates, caplog) -> None:
    backend = FakeBackend()
    with caplog.at_level("INFO"):
        failure = _run(run_quality_gates(backend, ["mystery/a.py"]))
    assert failure is None
    assert backend.commands == []
    assert "no configured commands" in caplog.text


# --- middleware wiring ------------------------------------------------------


def _wire(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> None:
    async def fake_get_backend(thread_id: str):
        return backend

    monkeypatch.setattr(qg, "get_sandbox_backend", fake_get_backend)
    monkeypatch.setattr(qg, "get_config", lambda: {"configurable": {"thread_id": "t-1"}})


def test_middleware_ignores_other_tools(demo_gates, monkeypatch) -> None:
    backend = FakeBackend()
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "handled"

    result = _run(QualityGatesMiddleware().awrap_tool_call(_request(tool="execute"), handler))
    assert result == "handled"
    assert backend.commands == []


def test_middleware_blocks_pr_on_gate_failure(demo_gates, monkeypatch) -> None:
    backend = FakeBackend(
        {
            "git diff --name-only": FakeResponse(output="demo/app/main.py\n"),
            "run-lint": FakeResponse(output="1 failed", exit_code=1),
        }
    )
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:  # pragma: no cover - must not be reached
        raise AssertionError("handler must not run when a gate fails")

    result = _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler))
    assert isinstance(result, ToolMessage)
    payload = json.loads(str(result.content))
    assert payload["code"] == "quality_gate_failed"
    assert payload["failing_command_name"] == "demo lint"
    assert payload["failure_kind"] == "failure"
    assert "1 failed" in payload["output_tail"]
    assert result.tool_call_id == "call-1"


def test_middleware_passes_pr_when_gates_green(demo_gates, monkeypatch) -> None:
    backend = FakeBackend({"git diff --name-only": FakeResponse(output="demo/app/main.py\n")})
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    assert _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler)) == "pr-opened"


def test_middleware_diffs_requested_head_branch(demo_gates, monkeypatch) -> None:
    """The gate diffs the requested PR head ref, not the sandbox's HEAD."""
    backend = FakeBackend({"git diff --name-only": FakeResponse(output="")})
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    request = _request(head="speedbay:feature-x")
    assert _run(QualityGatesMiddleware().awrap_tool_call(request, handler)) == "pr-opened"
    assert backend.commands[0].endswith("git diff --name-only origin/main...feature-x")


def test_middleware_fails_open_when_diff_unavailable(demo_gates, monkeypatch, caplog) -> None:
    backend = FakeBackend(
        {"git diff --name-only": FakeResponse(output="fatal: bad ref", exit_code=128)}
    )
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    with caplog.at_level("WARNING"):
        result = _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler))
    assert result == "pr-opened"
    assert "could not diff" in caplog.text


def test_middleware_fails_open_on_infrastructure_error(demo_gates, monkeypatch, caplog) -> None:
    async def broken_get_backend(thread_id: str):
        raise ConnectionError("sandbox down")

    monkeypatch.setattr(qg, "get_sandbox_backend", broken_get_backend)
    monkeypatch.setattr(qg, "get_config", lambda: {"configurable": {"thread_id": "t-1"}})

    async def handler(request: Any) -> Any:
        return "pr-opened"

    with caplog.at_level("ERROR"):
        result = _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler))
    assert result == "pr-opened"
    assert "gate infrastructure error" in caplog.text


# --- docker-backed integration (self-skips without daemon/image/creds) ------


def _docker_ready() -> bool:
    try:
        from agent.speedbay.docker_sandbox import validate_startup_config

        validate_startup_config()
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon or sandbox image unavailable")
def test_integration_gate_failure_and_pass_through_real_container(monkeypatch) -> None:
    """One seeded failing command and one passing command via the docker backend."""
    import subprocess

    from agent.speedbay.docker_sandbox import create_docker_sandbox

    sandbox = create_docker_sandbox()
    try:
        _run(sandbox.aexecute("mkdir -p /workspace/demo"))
        monkeypatch.setattr(
            qg,
            "PROJECT_QUALITY_GATES",
            {"demo": (GateCommand("seeded check", "test -f marker.txt"),)},
        )
        failing = _run(run_quality_gates(sandbox, ["demo/x.py"]))
        assert failing is not None and failing.command_name == "seeded check"

        _run(sandbox.aexecute("touch /workspace/demo/marker.txt"))
        assert _run(run_quality_gates(sandbox, ["demo/x.py"])) is None
    finally:
        subprocess.run(["docker", "rm", "-f", sandbox.id], capture_output=True)
