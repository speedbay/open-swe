"""Tests for the PR-standards gate middleware (OPE-8).

Unit tests drive the middleware through fake backends/requests (rule math is
tested in OPE-14's test_rules.py); one docker-gated integration test
seeds an oversized and a compliant change through the real docker backend and
self-skips where docker or the sandbox image is unavailable.

Run:  .venv/bin/python -m pytest tests/speedbay/test_pr_standards.py -x -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import attrs
import pytest
from langchain_core.messages import ToolMessage

from agent.speedbay import pr_standards as fg
from agent.speedbay.pr_standards import PRStandardsMiddleware

ISSUE = "OPE-8"
BRANCH = "ope-8-pr-standards"


@attrs.define(frozen=True)
class FakeResponse:
    output: str = ""
    exit_code: int | None = 0
    truncated: bool = False


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


def _body() -> str:
    return (
        f"Closes {ISSUE}\n\n"
        "## Why needed\nGate.\n\n"
        "## Solved / fixed\n- Gate.\n\n"
        "## Workflow enabled / fixed\nGate.\n\n"
        "## Verification\n- pytest\n"
    )


def _request(
    tool: str = "open_pull_request",
    *,
    title: str = f"{ISSUE}: add the gate",
    body: str | None = None,
    head: str = BRANCH,
    command: str = "",
) -> Any:
    """Duck-typed ToolCallRequest double (typed Any for the middleware calls)."""
    if tool == "execute":
        args: dict[str, str] = {"command": command}
    else:
        args = {"base": "main", "head": head, "title": title, "body": body or _body()}

    class Request:
        def __init__(self, tool_call: dict[str, Any] | None = None) -> None:
            self.tool_call = tool_call or {"name": tool, "args": args, "id": "call-1"}

        def override(self, *, tool_call: dict[str, Any]) -> Request:
            """Immutable-copy override, mirroring ToolCallRequest.override."""
            return Request(tool_call)

    return Request()


def _wire(
    monkeypatch: pytest.MonkeyPatch, backend: FakeBackend, *, issue: str | None = ISSUE
) -> None:
    async def fake_get_backend(thread_id: str):
        return backend

    configurable: dict[str, Any] = {"thread_id": "t-1"}
    if issue is not None:
        configurable["linear_issue"] = {"identifier": issue}
    monkeypatch.setattr(fg, "get_sandbox_backend", fake_get_backend)
    monkeypatch.setattr(fg, "get_config", lambda: {"configurable": configurable})


async def _pass_handler(request: Any) -> Any:
    return "pr-opened"


def _sync_handler(request: Any) -> ToolMessage:
    return ToolMessage(content="handled", tool_call_id="call-1")


async def _fail_handler(request: Any) -> Any:  # pragma: no cover - must not run
    raise AssertionError("handler must not run when the gate blocks")


def _numstat(rows: str) -> FakeBackend:
    return FakeBackend({"git diff --numstat": FakeResponse(output=rows)})


def _payload(result: Any) -> dict[str, Any]:
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call-1"
    return json.loads(str(result.content))


# --- shell-fallback interception (sync + async, pr_creation_guard parity) ----


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --title x",
        "git push || gh pr create --fill",
        "echo body | gh pr create --title x --body-file -",
        "gh api repos/o/r/pulls -f title=x",
        'bash -c "gh pr create"',
        "curl -X POST https://api.github.com/repos/o/r/pulls -d '{}'",
    ],
)
def test_shell_fallback_blocked_on_both_paths(command: str) -> None:
    middleware = PRStandardsMiddleware()
    request = _request(tool="execute", command=command)

    sync_result = middleware.wrap_tool_call(request, _sync_handler)
    payload = _payload(sync_result)
    assert payload["code"] == "pr_standards_fallback_blocked"
    assert payload["blocked_command"] == command

    async_result = _run(middleware.awrap_tool_call(request, _fail_handler))
    assert _payload(async_result)["code"] == "pr_standards_fallback_blocked"


def test_ordinary_execute_and_other_tools_pass_through() -> None:
    middleware = PRStandardsMiddleware()
    request = _request(tool="execute", command="git push origin main")
    sync_result = middleware.wrap_tool_call(request, _sync_handler)
    assert isinstance(sync_result, ToolMessage) and sync_result.content == "handled"
    assert _run(middleware.awrap_tool_call(request, _pass_handler)) == "pr-opened"


# --- the PR gate -------------------------------------------------------------


def test_compliant_diff_and_hygiene_open_normally(monkeypatch) -> None:
    backend = _numstat("10\t2\tagent/api.py\n40\t0\ttests/test_api.py\n")
    _wire(monkeypatch, backend)
    result = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler))
    assert result == "pr-opened"
    assert backend.commands[0].endswith(f"git diff --numstat origin/main...{BRANCH}")


def test_gate_passing_pr_is_forced_ready_for_review(monkeypatch) -> None:
    """OPE-34: a passing open_pull_request reaches the tool with draft=False,
    even when the model explicitly requested a draft — without mutating the
    original tool call (it is the same dict as the AIMessage history record)."""
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"))
    request = _request()
    request.tool_call["args"]["draft"] = True  # the upstream prompt's habit

    seen: dict[str, Any] = {}

    async def capture_handler(req: Any) -> Any:
        seen.update(req.tool_call["args"])
        return "pr-opened"

    assert _run(PRStandardsMiddleware().awrap_tool_call(request, capture_handler)) == "pr-opened"
    assert seen["draft"] is False
    # The original request's tool call (== the persisted AIMessage record)
    # must keep the model's actual argument.
    assert request.tool_call["args"]["draft"] is True


def test_blocked_pr_args_left_untouched(monkeypatch) -> None:
    """The draft rewrite applies only after the gate passes — a blocked call
    never reaches the tool, so its args are irrelevant but must not be
    silently rewritten (evidence in the block message stays faithful)."""
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    request = _request()
    _payload(_run(PRStandardsMiddleware().awrap_tool_call(request, _fail_handler)))
    assert "draft" not in request.tool_call["args"]


def test_oversized_diff_blocked_with_cap_and_split_instruction(monkeypatch) -> None:
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler)))
    assert payload["code"] == "pr_standards_failed"
    assert payload["recoverable_by_agent"] is True
    assert "effective LOC 400 exceeds the Track-A cap of 300" in payload["atomicity"]["exceeded"][0]
    assert "Split the change" in payload["error"]
    assert payload["atomicity"]["raw_loc"] == 400


def test_hygiene_violations_blocked_with_rule_names(monkeypatch) -> None:
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"))
    request = _request(title="update stuff", body=_body() + "\nMade by [Open SWE]\n")
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(request, _fail_handler)))
    rules = {v["rule"] for v in payload["hygiene_violations"]}
    assert {"title-format", "ai-attribution"} <= rules
    assert "[title-format]" in payload["error"]


def test_truncated_numstat_blocks_as_oversized(monkeypatch) -> None:
    backend = FakeBackend(
        {"git diff --numstat": FakeResponse(output="1\t0\tagent/api.py\n", truncated=True)}
    )
    _wire(monkeypatch, backend)
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler)))
    assert any("truncated" in reason for reason in payload["atomicity"]["exceeded"])


def test_no_linear_issue_runs_attribution_check_only(monkeypatch) -> None:
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"), issue=None)
    # Bad title passes without an expected issue id...
    request = _request(title="update stuff")
    assert _run(PRStandardsMiddleware().awrap_tool_call(request, _pass_handler)) == "pr-opened"
    # ...but AI attribution still blocks.
    request = _request(title="update stuff", body="Made by [Open SWE]")
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(request, _fail_handler)))
    assert [v["rule"] for v in payload["hygiene_violations"]] == ["ai-attribution"]


def test_corrective_rounds_bounded_then_escalates(monkeypatch) -> None:
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    middleware = PRStandardsMiddleware()
    for expected_round in (1, 2):
        payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
        assert payload["corrective_round"] == expected_round
        assert payload["escalation_required"] is False
    payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["corrective_round"] == 3
    assert payload["escalation_required"] is True
    assert payload["recoverable_by_agent"] is False
    assert "surface the gate failure to a human" in payload["error"]


def test_passing_gate_resets_the_round_counter(monkeypatch) -> None:
    middleware = PRStandardsMiddleware()
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"))
    assert _run(middleware.awrap_tool_call(_request(), _pass_handler)) == "pr-opened"
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    payload = _payload(_run(middleware.awrap_tool_call(_request(), _fail_handler)))
    assert payload["corrective_round"] == 1


def test_fails_open_when_diff_unavailable(monkeypatch, caplog) -> None:
    backend = FakeBackend(
        {"git diff --numstat": FakeResponse(output="fatal: bad ref", exit_code=128)}
    )
    _wire(monkeypatch, backend)
    with caplog.at_level("WARNING"):
        result = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler))
    assert result == "pr-opened"
    assert "could not diff" in caplog.text


def test_fails_open_on_infrastructure_error(monkeypatch, caplog) -> None:
    async def broken_get_backend(thread_id: str):
        raise ConnectionError("sandbox down")

    monkeypatch.setattr(fg, "get_sandbox_backend", broken_get_backend)
    monkeypatch.setattr(fg, "get_config", lambda: {"configurable": {"thread_id": "t-1"}})
    with caplog.at_level("ERROR"):
        result = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler))
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
def test_integration_oversized_blocks_and_compliant_passes(monkeypatch) -> None:
    """Seed one oversized and one compliant branch through the docker backend."""
    import subprocess

    from agent.speedbay.docker_sandbox import create_docker_sandbox

    sandbox = create_docker_sandbox()
    try:
        _run(
            sandbox.aexecute(
                "cd /workspace && git init -q -b main && git config user.email t@t "
                "&& git config user.name t && echo base > README.md "
                "&& git add . && git commit -qm base "
                f"&& git checkout -q -b {BRANCH} "
                "&& seq 400 > agent_big.py && git add . && git commit -qm big"
            )
        )

        async def real_backend(thread_id: str):
            return sandbox

        monkeypatch.setattr(fg, "get_sandbox_backend", real_backend)
        monkeypatch.setattr(
            fg,
            "get_config",
            lambda: {"configurable": {"thread_id": "t-1", "linear_issue": {"identifier": ISSUE}}},
        )

        blocked = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler))
        assert _payload(blocked)["code"] == "pr_standards_failed"

        _run(
            sandbox.aexecute(
                "cd /workspace && git checkout -q main && git checkout -q -b ope-8-small "
                "&& echo tweak >> README.md && git add . && git commit -qm small"
            )
        )
        result = _run(
            PRStandardsMiddleware().awrap_tool_call(_request(head="ope-8-small"), _pass_handler)
        )
        assert result == "pr-opened"
    finally:
        subprocess.run(["docker", "rm", "-f", sandbox.id], capture_output=True)
