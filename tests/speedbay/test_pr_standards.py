"""Tests for the PR-standards gate middleware (OPE-8, OPE-75).

Unit tests drive the middleware through fake backends/requests (rule math is
tested in OPE-14's test_rules.py). The first rule violation halts the run: the
gate returns ``Command(update={"messages": [...]}, goto=END)`` and the
``before_model`` backstop jumps to end — a compiled-agent regression proves no
second model call runs. One docker-gated integration test seeds an oversized
and a compliant change through the real docker backend and self-skips where
docker or the sandbox image is unavailable.

Run:  .venv/bin/python -m pytest tests/speedbay/test_pr_standards.py -x -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import attrs
import pytest
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.types import Command

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
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    *,
    issue: str | None = ISSUE,
    repo: Any = None,
) -> None:
    async def fake_get_backend(thread_id: str):
        return backend

    configurable: dict[str, Any] = {
        "thread_id": "t-1",
        "repo": repo if repo is not None else {"name": "wh"},
    }
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


def _commit_log(message: str | None = None) -> str:
    message = message or f"{ISSUE}: add the gate\n\n{_body()}"
    return f"{'a' * 40}\x1f{message}\x1e"


def _numstat(rows: str, *, commit_log: str | None = None) -> FakeBackend:
    # rev-parse resolves both refs to a pinned SHA first (the diff then runs
    # against SHAs); any 40-char value works for these unit fakes.
    return FakeBackend(
        {
            "rev-parse": FakeResponse(output="f" * 40),
            "diff --numstat": FakeResponse(output=rows),
            "log --format": FakeResponse(output=commit_log or _commit_log()),
        }
    )


def _payload(result: Any) -> dict[str, Any]:
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call-1"
    return json.loads(str(result.content))


def _halt_payload(result: Any) -> dict[str, Any]:
    """Assert the OPE-75 halt shape: Command ending the graph with the block."""
    assert isinstance(result, Command)
    assert result.goto == END
    assert isinstance(result.update, dict)
    (message,) = result.update["messages"]
    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-1"
    return json.loads(str(message.content))


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
    diff = next(c for c in backend.commands if "diff --numstat" in c)
    assert diff.startswith("git -C /workspace/wh diff --numstat")
    # The diff runs against pinned SHAs (resolved first), never mutable refs.
    assert diff.endswith(f"{'f' * 40}...{'f' * 40}")


def test_all_compliant_branch_commits_pass(monkeypatch) -> None:
    first = f"{'a' * 40}\x1f{ISSUE}: add the gate\n\n{_body()}"
    second = f"{'b' * 40}\x1f{ISSUE}: test the gate\n\n{_body()}"
    backend = _numstat("1\t0\tagent/api.py\n", commit_log=f"{first}\x1e{second}\x1e")
    _wire(monkeypatch, backend)
    assert _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler)) == "pr-opened"


def test_bad_commit_headline_blocks_compliant_pr_metadata(monkeypatch) -> None:
    backend = _numstat(
        "1\t0\tagent/api.py\n",
        commit_log=_commit_log(f"Closes {ISSUE}\n\n{_body()}"),
    )
    _wire(monkeypatch, backend)
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler)))
    assert payload["code"] == "pr_standards_hygiene_retry"
    assert [finding["rule"] for finding in payload["findings"]] == ["commit-title-format"]
    assert "Amend or reword each named commit message" in payload["error"]
    assert "do not amend commits" not in payload["error"]


def test_non_dict_repo_config_keeps_hygiene_enforced(monkeypatch) -> None:
    backend = _numstat("1\t0\tagent/api.py\n")
    backend.script["ls -d"] = FakeResponse(output="/workspace/wh/.git\n")
    _wire(monkeypatch, backend, repo="malformed")

    payload = _payload(
        _run(PRStandardsMiddleware().awrap_tool_call(_request(title="update stuff"), _fail_handler))
    )

    assert payload["code"] == "pr_standards_hygiene_retry"
    assert [finding["rule"] for finding in payload["findings"]] == ["title-format"]


@pytest.mark.parametrize("subject", ["Merge branch 'main'", f'Revert "{ISSUE}: add the gate"'])
def test_merge_and_revert_subjects_are_not_exempt(monkeypatch, subject: str) -> None:
    backend = _numstat("1\t0\tagent/api.py\n", commit_log=_commit_log(subject))
    _wire(monkeypatch, backend)
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler)))
    assert [finding["rule"] for finding in payload["findings"]] == [
        "commit-title-format",
        "commit-closes-line",
    ]


@pytest.mark.parametrize(
    ("proven", "commit", "rules"),
    [
        (True, "upstream merge subject\n\nMade by [Open SWE]", []),
        (True, "upstream merge subject\n\ngit add -A", ["commit-explicit-staging"]),
        (
            False,
            "upstream merge subject\n\nMade by [Open SWE]",
            ["commit-title-format", "commit-ai-attribution", "commit-closes-line"],
        ),
        (False, "Merge branch 'main'", ["commit-title-format", "commit-closes-line"]),
    ],
)
def test_upstream_provenance_exempts_only_proven_rules(
    monkeypatch, proven: bool, commit: str, rules: list[str]
) -> None:
    sha = "a" * 40
    scripts = {
        "rev-parse": FakeResponse(output="f" * 40),
        "diff --numstat": FakeResponse(output="1\t0\tagent/api.py\n"),
        "log --format": FakeResponse(output=f"{sha}\x1f{commit}\x1e"),
        "fetch --no-tags": FakeResponse(),
        "rev-list --parents": FakeResponse(output=f"{'m' * 40} {'b' * 40} {'p' * 40}\n"),
        "merge-base --is-ancestor": FakeResponse(exit_code=0 if proven else 1),
    }
    backend = FakeBackend(scripts)
    _wire(monkeypatch, backend, repo={"owner": "speedbay", "name": "open-swe"})
    result = _run(
        PRStandardsMiddleware().awrap_tool_call(
            _request(), _fail_handler if rules else _pass_handler
        )
    )
    assert any(
        "https://github.com/langchain-ai/open-swe.git" in command for command in backend.commands
    )
    if not rules:
        assert result == "pr-opened"
        return
    assert {finding["rule"] for finding in _payload(result)["findings"]} == set(rules)


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
    _stub_gate_store(monkeypatch)
    request = _request()
    _halt_payload(_run(PRStandardsMiddleware().awrap_tool_call(request, _fail_handler)))
    assert "draft" not in request.tool_call["args"]


def test_oversized_diff_halts_with_cap_and_split_instruction(monkeypatch) -> None:
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    _stub_gate_store(monkeypatch)
    payload = _halt_payload(
        _run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler))
    )
    assert payload["code"] == "pr_standards_failed"
    assert payload["recoverable_by_agent"] is False
    assert payload["disposition"] == "human_decision"
    assert "effective LOC 400 exceeds the Track-A cap of 300" in payload["atomicity"]["exceeded"][0]
    assert "Split the change" in payload["error"]
    assert payload["atomicity"]["raw_loc"] == 400
    assert payload["gate_approval"]["status"] == "pending"
    assert all(f["severity"] == "hard" for f in payload["findings"])


def test_malformed_numstat_halts_with_durable_parsing_evidence(monkeypatch, caplog) -> None:
    malformed = "1\t0\tagent/api.py\textra\n"
    _wire(monkeypatch, _numstat(malformed))
    _stub_gate_store(monkeypatch)
    durable: dict[str, Any] = {}

    async def ensure(thread_id: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        durable.update(kwargs)
        return {"status": "pending", **kwargs}, True

    monkeypatch.setattr(fg, "ensure_gate_approval_pending", ensure)
    handled: list[bool] = []

    async def handler(request: Any) -> Any:
        handled.append(True)
        return "pr-opened"

    with caplog.at_level("ERROR"):
        payload = _halt_payload(_run(PRStandardsMiddleware().awrap_tool_call(_request(), handler)))

    assert handled == []
    assert payload["atomicity"]["passed"] is False
    assert payload["atomicity"]["exceeded"] == [
        "malformed numstat row: expected exactly two tab separators"
    ]
    assert payload["findings"] == [
        {
            "domain": "atomicity",
            "rule": "atomicity",
            "message": "malformed numstat row: expected exactly two tab separators",
            "severity": "hard",
        }
    ]
    assert "Atomicity numstat parsing failed" in payload["error"]
    assert "malformed numstat row" in payload["error"]
    assert durable["evidence_tail"] == malformed
    assert durable["diff_stats"]["exceeded"] == payload["atomicity"]["exceeded"]
    assert "gate infrastructure error" not in caplog.text


def test_hygiene_only_violations_get_corrective_retry(monkeypatch) -> None:
    """OPE-75: hygiene is REMEDIABLE — an agent-recoverable corrective block
    embedding the required format, no Command, no approval card."""
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"))
    request = _request(title="update stuff", body=_body() + "\nMade by [Open SWE]\n")
    payload = _payload(_run(PRStandardsMiddleware().awrap_tool_call(request, _fail_handler)))
    assert payload["code"] == "pr_standards_hygiene_retry"
    assert payload["recoverable_by_agent"] is True
    assert payload["disposition"] == "agent_remediation"
    assert "gate_approval" not in payload  # no card, no fingerprint
    rules = {f["rule"] for f in payload["findings"]}
    assert {"title-format", "ai-attribution"} <= rules
    assert all(f["severity"] == "remediable" for f in payload["findings"])
    assert "[title-format]" in payload["error"]
    assert "## Why needed" in payload["error"]  # the literal format is embedded


def test_hygiene_budget_exhaustion_escalates_to_approval(monkeypatch) -> None:
    """The third consecutive hygiene-only failure ends the run through the
    approval path; finding severity still reports 'remediable'."""
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"))
    _stub_gate_store(monkeypatch)
    middleware = PRStandardsMiddleware()
    request = _request(title="update stuff")
    for _ in range(2):
        payload = _payload(_run(middleware.awrap_tool_call(request, _fail_handler)))
        assert payload["code"] == "pr_standards_hygiene_retry"
    payload = _halt_payload(_run(middleware.awrap_tool_call(request, _fail_handler)))
    assert payload["code"] == "pr_standards_failed"
    assert payload["recoverable_by_agent"] is False
    assert payload["disposition"] == "escalated_after_remediation_budget"
    assert payload["gate_approval"]["status"] == "pending"
    # Classification never mutates: the escalated findings stay remediable.
    assert all(f["severity"] == "remediable" for f in payload["findings"])


def test_passing_gate_resets_hygiene_budget(monkeypatch) -> None:
    middleware = PRStandardsMiddleware()
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n"))
    bad = _request(title="update stuff")
    for _ in range(2):
        _payload(_run(middleware.awrap_tool_call(bad, _fail_handler)))
    assert _run(middleware.awrap_tool_call(_request(), _pass_handler)) == "pr-opened"
    # The pass cleared the counter: the next failure is attempt 1, not exhaustion.
    payload = _payload(_run(middleware.awrap_tool_call(bad, _fail_handler)))
    assert payload["code"] == "pr_standards_hygiene_retry"


def test_mixed_atomicity_and_hygiene_halts_immediately(monkeypatch) -> None:
    """A HARD finding dominates: mixed failures never get a hygiene retry."""
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    _stub_gate_store(monkeypatch)
    request = _request(title="update stuff")
    payload = _halt_payload(_run(PRStandardsMiddleware().awrap_tool_call(request, _fail_handler)))
    assert payload["disposition"] == "human_decision"
    severities = {f["rule"]: f["severity"] for f in payload["findings"]}
    assert severities["atomicity"] == "hard"
    assert severities["title-format"] == "remediable"


def test_truncated_numstat_halts_as_oversized(monkeypatch) -> None:
    backend = FakeBackend(
        {
            "rev-parse": FakeResponse(output="f" * 40),
            "diff --numstat": FakeResponse(output="1\t0\tagent/api.py\n", truncated=True),
        }
    )
    _wire(monkeypatch, backend)
    _stub_gate_store(monkeypatch)
    payload = _halt_payload(
        _run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler))
    )
    assert any("truncated" in reason for reason in payload["atomicity"]["exceeded"])


@pytest.mark.parametrize(
    ("pr_text", "commit_text", "rules"),
    [
        ("Made by [Open SWE]", None, ["ai-attribution"]),
        ("ran git add -A", None, ["explicit-staging"]),
        (None, "Generated by Claude Code", ["commit-ai-attribution"]),
        (None, "ran git add -A", ["commit-explicit-staging"]),
        ("malformed metadata but no universal violation", None, []),
    ],
)
def test_no_linear_issue_checks_universal_pr_and_commit_hygiene(
    monkeypatch, pr_text: str | None, commit_text: str | None, rules: list[str]
) -> None:
    commit_log = _commit_log(commit_text) if commit_text is not None else None
    _wire(monkeypatch, _numstat("1\t0\tagent/api.py\n", commit_log=commit_log), issue=None)
    request = _request(title="update stuff", body=pr_text or "bad body")
    result = _run(
        PRStandardsMiddleware().awrap_tool_call(request, _fail_handler if rules else _pass_handler)
    )
    if not rules:
        assert result == "pr-opened"
        return
    payload = _payload(result)
    assert [finding["rule"] for finding in payload["findings"]] == rules
    assert payload["code"] == "pr_standards_hygiene_retry"


def _stub_gate_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Working durable-store stubs: a fresh pending record, nothing approved.

    Keeps these unit tests focused on rule verdicts and the halt shape; full
    store behavior (record-before-notify, exemption consumption, rejection)
    is covered in test_gate_approval.py.
    """

    async def ensure(thread_id: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        return {"status": "pending", **kwargs}, True

    async def status(thread_id: str, fingerprint: str) -> str:
        return "pending"

    async def consume(thread_id: str, fingerprint: str) -> bool:
        return False

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def comment(issue_id: str, body: str, parent_id: str | None = None) -> bool:
        return True

    monkeypatch.setattr(fg, "ensure_gate_approval_pending", ensure)
    monkeypatch.setattr(fg, "gate_approval_status", status)
    monkeypatch.setattr(fg, "consume_gate_approval", consume)
    monkeypatch.setattr(fg, "mark_gate_approval_notified", noop)
    monkeypatch.setattr(fg, "comment_on_linear_issue", comment)


def test_store_fault_after_rule_verdict_fails_closed_and_ends_run(monkeypatch, caplog) -> None:
    """OPE-75: a durable-store fault after a rule verdict must not permit PR
    creation or another corrective turn — the run ends with explicit
    infrastructure evidence."""
    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no durable store")

    monkeypatch.setattr(fg, "ensure_gate_approval_pending", fail)
    with caplog.at_level("ERROR"):
        payload = _halt_payload(
            _run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler))
        )
    assert payload["code"] == "pr_standards_store_error"
    assert payload["recoverable_by_agent"] is False
    assert "gate-approval state store failed" in payload["error"]
    assert "run has ended without opening a PR" in payload["error"]
    assert "durable approval state error" in caplog.text


def test_violating_open_pull_request_ends_run_without_second_model_call(monkeypatch) -> None:
    """Compiled-agent regression (OPE-75 AC 1): a violating open_pull_request
    never reaches its handler and the graph ends without a second model call."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool

    _wire(monkeypatch, _numstat("400\t0\tagent/api.py\n"))
    _stub_gate_store(monkeypatch)

    model_calls = {"count": 0}

    class FakeToolCallingModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolCallingModel:
            return self

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            model_calls["count"] += 1
            return super()._generate(*args, **kwargs)

    handled: list[bool] = []

    @tool
    async def open_pull_request(title: str, body: str, head: str, base: str) -> str:
        """Open a pull request."""
        handled.append(True)
        return "opened"

    messages = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "open_pull_request",
                        "args": {
                            "title": f"{ISSUE}: add the gate",
                            "body": _body(),
                            "head": BRANCH,
                            "base": "main",
                        },
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="a second model turn must never run"),
        ]
    )
    agent = create_agent(
        model=FakeToolCallingModel(messages=messages),
        tools=[open_pull_request],
        middleware=[PRStandardsMiddleware()],
    )
    result = _run(agent.ainvoke({"messages": [HumanMessage(content="open the PR")]}))

    assert model_calls["count"] == 1  # the tool-calling turn only
    assert handled == []  # the PR tool handler was never called
    last = result["messages"][-1]
    assert isinstance(last, ToolMessage)
    payload = json.loads(str(last.content))
    assert payload["code"] == "pr_standards_failed"
    assert payload["recoverable_by_agent"] is False


def test_fails_open_when_diff_unavailable(monkeypatch, caplog) -> None:
    backend = FakeBackend({"diff --numstat": FakeResponse(output="fatal: bad ref", exit_code=128)})
    _wire(monkeypatch, backend)
    with caplog.at_level("ERROR"):
        result = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler))
    assert result == "pr-opened"
    assert "could not diff" in caplog.text


def test_fails_open_when_commit_log_unavailable(monkeypatch, caplog) -> None:
    backend = _numstat("1\t0\tagent/api.py\n")
    backend.script["log --format"] = FakeResponse(output="fatal: bad range", exit_code=128)
    _wire(monkeypatch, backend)
    with caplog.at_level("ERROR"):
        result = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler))
    assert result == "pr-opened"
    assert "could not inspect commits" in caplog.text


def test_commit_log_failure_preserves_atomicity_verdict(monkeypatch) -> None:
    backend = _numstat("400\t0\tagent/api.py\n")
    backend.script["log --format"] = FakeResponse(output="fatal: bad range", exit_code=128)
    _wire(monkeypatch, backend)
    _stub_gate_store(monkeypatch)
    payload = _halt_payload(
        _run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler))
    )
    assert payload["atomicity"]["passed"] is False
    assert [finding["rule"] for finding in payload["findings"]] == ["atomicity"]


def test_fails_open_loudly_when_no_repo_clone_found(monkeypatch, caplog) -> None:
    """The pre-OPE-59 production outage shape: no clone at the diffed path.

    Resolution failure must pass fail-open (OPE-9) but at error level — two
    silent warnings were the only trace of a fully disabled gate.
    """
    backend = FakeBackend(
        {
            "rev-parse": FakeResponse(output="", exit_code=128),
            "ls -d": FakeResponse(output="", exit_code=1),
        }
    )
    _wire(monkeypatch, backend)
    with caplog.at_level("ERROR"):
        result = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _pass_handler))
    assert result == "pr-opened"
    assert "no repo clone found" in caplog.text


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
        # Real clone layout (OPE-59): the repo lives at /workspace/<name>, the
        # way `gh repo clone` leaves it — /workspace itself is not a git repo.
        # Seeding at /workspace root is exactly the layout that masked the
        # production fail-open outage; this test must never use it again.
        _run(
            sandbox.aexecute(
                "mkdir -p /workspace/wh && cd /workspace/wh "
                "&& git init -q -b main && git config user.email t@t "
                "&& git config user.name t && echo base > README.md "
                "&& git add . && git commit -qm base -m 'Closes T-1' "
                f"&& git checkout -q -b {BRANCH} "
                "&& seq 400 > agent_big.py && git add . && git commit -qm big -m 'Closes T-1'"
            )
        )

        async def real_backend(thread_id: str):
            return sandbox

        monkeypatch.setattr(fg, "get_sandbox_backend", real_backend)
        monkeypatch.setattr(
            fg,
            "get_config",
            lambda: {
                "configurable": {
                    "thread_id": "t-1",
                    "repo": {"name": "wh"},
                    "linear_issue": {"identifier": ISSUE},
                }
            },
        )

        _stub_gate_store(monkeypatch)
        blocked = _run(PRStandardsMiddleware().awrap_tool_call(_request(), _fail_handler))
        assert _halt_payload(blocked)["code"] == "pr_standards_failed"

        _run(
            sandbox.aexecute(
                "cd /workspace/wh && git checkout -q main && git checkout -q -b ope-8-small "
                "&& echo tweak >> README.md && git add . && git commit -qm small -m 'Closes T-1'"
            )
        )
        result = _run(
            PRStandardsMiddleware().awrap_tool_call(_request(head="ope-8-small"), _pass_handler)
        )
        assert result == "pr-opened"
    finally:
        subprocess.run(["docker", "rm", "-f", sandbox.id], capture_output=True)
