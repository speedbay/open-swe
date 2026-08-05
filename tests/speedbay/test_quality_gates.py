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

import attrs
import pytest
from langchain_core.messages import ToolMessage

from agent.speedbay import quality_gates as qg
from agent.speedbay.quality_gates import (
    GateCommand,
    QualityGatesMiddleware,
    run_quality_gates,
    touched_projects,
)


@attrs.define(frozen=True)
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


# Resolved repo clone under /workspace (OPE-59): the agent clones into
# /workspace/<repo>, so gates never run at /workspace itself.
REPO_DIR = "/workspace/wh"


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


def test_baydoor_build_gates_use_ci_placeholder_environment() -> None:
    gates = {gate.name: gate.command for gate in qg.PROJECT_QUALITY_GATES["baydoor"]}
    placeholder_names = {
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
        "DATABRICKS_CLIENT_ID",
        "LAKEBASE_ENDPOINT",
        "PGDATABASE",
        "PGHOST",
    }

    for gate_name in ("build", "frontend render check"):
        assert all(f"{name}=" in gates[gate_name] for name in placeholder_names)
    # render:check boots the app: Clerk middleware needs a valid-format fake
    # secret too (OPE-92 follow-up; build proved it does not need one).
    assert "CLERK_SECRET_KEY=" in gates["frontend render check"]
    assert "CLERK_SECRET_KEY=" not in gates["build"]
    # render:check drives a real browser: pk_test_ selects a Clerk development
    # instance whose middleware 307-redirects document requests to the
    # unresolvable clerk.example.com handshake, so the render gate uses
    # production-instance (pk_live_/sk_live_) placeholders while build stays
    # pk_test_ in sync with warehouse ci.yml (OPE-94).
    assert "pk_live_" in gates["frontend render check"]
    assert "sk_live_" in gates["frontend render check"]
    assert "pk_test_" in gates["build"]
    assert "pk_live_" not in gates["build"]
    for gate_name in ("install dependencies", "eslint", "test", "typecheck"):
        assert not any(f"{name}=" in gates[gate_name] for name in placeholder_names)
        assert "CLERK_SECRET_KEY=" not in gates[gate_name]


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
        assert _run(run_quality_gates(backend, paths, REPO_DIR)) is None
        for gate in gates:
            assert gate.name and gate.command
            assert any(gate.command in issued for issued in backend.commands), (
                project,
                gate.name,
            )


# --- gate runner ------------------------------------------------------------


def test_commands_run_from_project_root_and_pass(demo_gates) -> None:
    """Gate commands run inside <repo_dir>/<project>, not /workspace/<project>."""
    backend = FakeBackend()
    failure = _run(run_quality_gates(backend, ["demo/app/main.py"], REPO_DIR))
    assert failure is None
    assert backend.commands == [
        "cd /workspace/wh/demo && set -o pipefail && ( run-lint ) 2>&1 | tail -c 2000",
        "cd /workspace/wh/demo && set -o pipefail && ( run-scoped ) 2>&1 | tail -c 2000",
    ]


def test_path_filtered_gate_skipped_when_no_match(demo_gates) -> None:
    backend = FakeBackend()
    failure = _run(run_quality_gates(backend, ["demo/docs/readme.md"], REPO_DIR))
    assert failure is None
    assert backend.commands == [
        "cd /workspace/wh/demo && set -o pipefail && ( run-lint ) 2>&1 | tail -c 2000"
    ]


def test_failing_command_blocks_with_name_and_bounded_tail(demo_gates) -> None:
    long_output = "x" * 5000 + "TAIL-MARKER"
    backend = FakeBackend({"run-lint": FakeResponse(output=long_output, exit_code=1)})
    failure = _run(run_quality_gates(backend, ["demo/app/main.py"], REPO_DIR))
    assert failure is not None
    assert failure.command_name == "demo lint"
    assert failure.kind == "failure"
    assert failure.output_tail.endswith("TAIL-MARKER")
    assert len(failure.output_tail) == qg.OUTPUT_TAIL_CHARS
    # first failure stops the run — the scoped command never executed
    assert len(backend.commands) == 1 and "run-lint" in backend.commands[0]
    # the true tail is captured command-side, before backend head-truncation
    assert backend.commands[0].endswith(f"| tail -c {qg.OUTPUT_TAIL_CHARS}")


def test_precondition_failure_distinguished_by_exit_127(demo_gates) -> None:
    backend = FakeBackend(
        {"run-lint": FakeResponse(output="sh: run-lint: not found", exit_code=127)}
    )
    failure = _run(run_quality_gates(backend, ["demo/a.py"], REPO_DIR))
    assert failure is not None and failure.kind == "precondition"


def test_precondition_failure_distinguished_by_marker(demo_gates) -> None:
    backend = FakeBackend({"run-lint": FakeResponse(output="bash: uv: not found", exit_code=1)})
    failure = _run(run_quality_gates(backend, ["demo/a.py"], REPO_DIR))
    assert failure is not None and failure.kind == "precondition"


def test_timeout_exit_code_classified(demo_gates) -> None:
    backend = FakeBackend({"run-lint": FakeResponse(output="", exit_code=124)})
    failure = _run(run_quality_gates(backend, ["demo/a.py"], REPO_DIR))
    assert failure is not None and failure.kind == "timeout"


def test_unconfigured_project_passes_with_notice(demo_gates, caplog) -> None:
    backend = FakeBackend()
    with caplog.at_level("INFO"):
        failure = _run(run_quality_gates(backend, ["mystery/a.py"], REPO_DIR))
    assert failure is None
    assert backend.commands == []
    assert "no configured commands" in caplog.text


# --- middleware wiring ------------------------------------------------------


def _wire(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> None:
    async def fake_get_backend(thread_id: str):
        return backend

    monkeypatch.setattr(qg, "get_sandbox_backend", fake_get_backend)
    monkeypatch.setattr(
        qg,
        "get_config",
        lambda: {"configurable": {"thread_id": "t-1", "repo": {"name": "wh"}}},
    )


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
            "diff --name-only": FakeResponse(output="demo/app/main.py\n"),
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
    backend = FakeBackend({"diff --name-only": FakeResponse(output="demo/app/main.py\n")})
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    assert _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler)) == "pr-opened"


def test_middleware_diffs_requested_head_branch(demo_gates, monkeypatch) -> None:
    """The gate diffs the requested PR head ref, not the sandbox's HEAD."""
    backend = FakeBackend({"diff --name-only": FakeResponse(output="")})
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    request = _request(head="speedbay:feature-x")
    assert _run(QualityGatesMiddleware().awrap_tool_call(request, handler)) == "pr-opened"
    # commands[0] is the resolve_repo_dir probe; the diff targets the clone.
    diff = next(c for c in backend.commands if "diff --name-only" in c)
    assert diff.startswith("git -C /workspace/wh diff --name-only")
    assert diff.endswith("origin/main...feature-x")


def test_diff_refs_are_shell_quoted_against_hostile_branch_names(demo_gates, monkeypatch) -> None:
    """Metacharacter refs cannot break out of the git diff invocation.

    An unquoted ``feature;true`` would make the shell run ``true`` after the
    failed diff — exit 0 with no paths, silently skipping every gate.
    """
    backend = FakeBackend({"diff --name-only": FakeResponse(output="")})
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    request = _request(base="main;rm -rf /", head="feature;true")
    assert _run(QualityGatesMiddleware().awrap_tool_call(request, handler)) == "pr-opened"
    diff = next(c for c in backend.commands if "diff --name-only" in c)
    assert diff.endswith("diff --name-only 'origin/main;rm -rf /'...'feature;true'")


def test_middleware_fails_open_when_diff_unavailable(demo_gates, monkeypatch, caplog) -> None:
    backend = FakeBackend(
        {"diff --name-only": FakeResponse(output="fatal: bad ref", exit_code=128)}
    )
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    with caplog.at_level("ERROR"):
        result = _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler))
    assert result == "pr-opened"
    assert "could not diff" in caplog.text


# --- head-tree materialization (OPE-95) --------------------------------------


def _head_script(clone_state: str) -> dict[str, FakeResponse]:
    """Scripted backend for a head resolving to commit ``bbb``.

    ``clone_state`` is the combined ``rev-parse HEAD && status --porcelain``
    output: the clone's commit plus any dirty-file lines.
    """
    return {
        "rev-parse --verify": FakeResponse(output="bbb\n"),
        "rev-parse HEAD &&": FakeResponse(output=clone_state),
        "diff --name-only": FakeResponse(output="demo/app/main.py\n"),
    }


async def _opened(request: Any) -> Any:
    return "pr-opened"


def test_gates_run_in_head_worktree_when_clone_differs(demo_gates, monkeypatch) -> None:
    """A clone at another commit gates an ephemeral worktree of the head.

    The OPE-95 incident shape: the primary clone sits on a stale commit while
    the PR branch lives elsewhere — gate commands must cd into the head's
    worktree, never the stale tree.
    """
    backend = FakeBackend(_head_script("aaa\n"))
    _wire(monkeypatch, backend)

    assert _run(QualityGatesMiddleware().awrap_tool_call(_request(head="feature-x"), _opened)) == "pr-opened"
    gate = next(c for c in backend.commands if "run-lint" in c)
    assert gate.startswith("cd /tmp/gate-t-1/demo && ")
    assert any("worktree add --detach /tmp/gate-t-1 bbb" in c for c in backend.commands)
    after_gate = backend.commands[backend.commands.index(gate) + 1 :]
    assert any("worktree remove --force /tmp/gate-t-1" in c for c in after_gate)


def test_gates_run_in_place_when_clone_at_head_and_clean(demo_gates, monkeypatch) -> None:
    """A clean clone already at the head commit is the head tree: no worktree."""
    backend = FakeBackend(_head_script("bbb\n"))
    _wire(monkeypatch, backend)

    assert _run(QualityGatesMiddleware().awrap_tool_call(_request(head="feature-x"), _opened)) == "pr-opened"
    gate = next(c for c in backend.commands if "run-lint" in c)
    assert gate.startswith("cd /workspace/wh/demo && ")
    assert not any("worktree add" in c for c in backend.commands)


def test_gates_go_ephemeral_when_clone_at_head_but_dirty(demo_gates, monkeypatch) -> None:
    """Uncommitted edits differ from the pushed head: gate the worktree."""
    backend = FakeBackend(_head_script("bbb\n M demo/app/main.py\n"))
    _wire(monkeypatch, backend)

    assert _run(QualityGatesMiddleware().awrap_tool_call(_request(head="feature-x"), _opened)) == "pr-opened"
    gate = next(c for c in backend.commands if "run-lint" in c)
    assert gate.startswith("cd /tmp/gate-t-1/demo && ")


def test_gate_worktree_removed_when_gate_fails(demo_gates, monkeypatch) -> None:
    """The ephemeral worktree never outlives the run, pass or fail."""
    backend = FakeBackend(
        {**_head_script("aaa\n"), "run-lint": FakeResponse(output="1 failed", exit_code=1)}
    )
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:  # pragma: no cover - gate must block
        raise AssertionError("handler must not run when a gate fails")

    result = _run(QualityGatesMiddleware().awrap_tool_call(_request(head="feature-x"), handler))
    assert isinstance(result, ToolMessage)
    gate = next(c for c in backend.commands if "run-lint" in c)
    after_gate = backend.commands[backend.commands.index(gate) + 1 :]
    assert any("worktree remove --force /tmp/gate-t-1" in c for c in after_gate)


def test_middleware_fails_open_when_head_unresolvable(demo_gates, monkeypatch, caplog) -> None:
    """A head that resolves nowhere keeps the gate's fail-open contract."""
    backend = FakeBackend(
        {
            "diff --name-only": FakeResponse(output="demo/app/main.py\n"),
            "rev-parse --verify": FakeResponse(output="fatal: bad ref", exit_code=128),
        }
    )
    _wire(monkeypatch, backend)

    with caplog.at_level("ERROR"):
        result = _run(QualityGatesMiddleware().awrap_tool_call(_request(head="gone"), _opened))
    assert result == "pr-opened"
    assert "could not materialize" in caplog.text


# --- repo-dir resolution (OPE-59) --------------------------------------------


def test_resolve_repo_dir_prefers_declared_name() -> None:
    """The issue-declared repo (configurable['repo'], OPE-49) wins outright."""
    backend = FakeBackend()
    resolved = _run(qg.resolve_repo_dir(backend, {"repo": {"name": "warehouse"}}))
    assert resolved == "/workspace/warehouse"
    assert backend.commands == ["git -C /workspace/warehouse rev-parse --is-inside-work-tree"]


def test_resolve_repo_dir_falls_back_to_single_clone_discovery() -> None:
    """No declaration → exactly one /workspace/*/.git resolves to its parent."""
    backend = FakeBackend({"ls -d": FakeResponse(output="/workspace/only/.git\n")})
    assert _run(qg.resolve_repo_dir(backend, {})) == "/workspace/only"


def test_resolve_repo_dir_none_when_ambiguous_or_empty() -> None:
    """Zero or multiple clones cannot be guessed — resolution reports None."""
    two = FakeBackend({"ls -d": FakeResponse(output="/workspace/a/.git\n/workspace/b/.git\n")})
    assert _run(qg.resolve_repo_dir(two, {})) is None
    none = FakeBackend({"ls -d": FakeResponse(output="", exit_code=1)})
    assert _run(qg.resolve_repo_dir(none, {})) is None


def test_resolve_repo_dir_quotes_hostile_declared_name() -> None:
    """A metacharacter repo name cannot break out of the probe command."""
    backend = FakeBackend({"rev-parse": FakeResponse(output="", exit_code=128)})
    _run(qg.resolve_repo_dir(backend, {"repo": {"name": "x;rm -rf /"}}))
    assert backend.commands[0].startswith("git -C '/workspace/x;rm -rf /' rev-parse")


def test_middleware_fails_open_when_no_repo_clone_found(demo_gates, monkeypatch, caplog) -> None:
    """Unresolvable clone → fail-open pass, but loudly (error level, OPE-59)."""
    backend = FakeBackend(
        {
            "rev-parse": FakeResponse(output="", exit_code=128),
            "ls -d": FakeResponse(output="", exit_code=1),
        }
    )
    _wire(monkeypatch, backend)

    async def handler(request: Any) -> Any:
        return "pr-opened"

    with caplog.at_level("ERROR"):
        result = _run(QualityGatesMiddleware().awrap_tool_call(_request(), handler))
    assert result == "pr-opened"
    assert "no repo clone found" in caplog.text


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
        # Real clone layout (OPE-59): the repo is a git worktree under
        # /workspace/<name>, with the project directory inside the clone.
        _run(sandbox.aexecute("mkdir -p /workspace/wh/demo && git -C /workspace/wh init -q"))
        resolved = _run(qg.resolve_repo_dir(sandbox, {"repo": {"name": "wh"}}))
        assert resolved == "/workspace/wh"
        monkeypatch.setattr(
            qg,
            "PROJECT_QUALITY_GATES",
            {"demo": (GateCommand("seeded check", "test -f marker.txt"),)},
        )
        failing = _run(run_quality_gates(sandbox, ["demo/x.py"], resolved))
        assert failing is not None and failing.command_name == "seeded check"

        _run(sandbox.aexecute("touch /workspace/wh/demo/marker.txt"))
        assert _run(run_quality_gates(sandbox, ["demo/x.py"], resolved)) is None

        # OPE-95: through the middleware, a primary checkout on an older
        # commit must gate the requested head's tree — the probe content
        # exists only on the branch, so a pass proves the head was validated.
        _run(
            sandbox.aexecute(
                "cd /workspace/wh && git config user.email t@t && git config user.name t"
                " && git checkout -qb main && echo old > demo/probe.txt && git add -A"
                " && git commit -qm one && git checkout -qb feature"
                " && echo new > demo/probe.txt && git commit -qam two"
                " && git checkout -q main"
            )
        )
        monkeypatch.setattr(
            qg,
            "PROJECT_QUALITY_GATES",
            {"demo": (GateCommand("head probe", "grep -q new probe.txt"),)},
        )

        async def fake_get_backend(thread_id: str):
            return sandbox

        monkeypatch.setattr(qg, "get_sandbox_backend", fake_get_backend)
        monkeypatch.setattr(
            qg,
            "get_config",
            lambda: {"configurable": {"thread_id": "t-int", "repo": {"name": "wh"}}},
        )
        result = _run(QualityGatesMiddleware().awrap_tool_call(_request(head="feature"), _opened))
        assert result == "pr-opened"
        leftovers = _run(sandbox.aexecute("ls -d /tmp/gate-* 2>/dev/null"))
        assert not (getattr(leftovers, "output", "") or "").strip()
    finally:
        subprocess.run(["docker", "rm", "-f", sandbox.id], capture_output=True)
