"""Per-project quality gates before ``open_pull_request`` (OPE-9).

SPEEDBAY org-layer file — upstream does not own it.

Before a PR opens, run the touched project's CI-equivalent validation commands
(tests/lint/typecheck) inside the sandbox and block the tool call on failure —
the host-owned quality gate (warehouse ADR-010) reborn as ``wrap_tool_call``
middleware, following the interception pattern of
``agent/middleware/pr_creation_guard.py``.

Command source of truth: the per-project ``quality_gates`` lists in warehouse
root ``workflow.md``. The fork must not read warehouse files at runtime, so the
commands are copied into ``PROJECT_QUALITY_GATES`` below with provenance;
re-sync the map when workflow.md changes.

Fail-open by design for *infrastructure* problems (no thread id, unreachable
sandbox, undiffable base): a broken gate must not permanently block PR
creation — those cases log and let the tool proceed. Command failures and
precondition failures (missing deps/tools) block with a corrective
ToolMessage; the two are labelled differently because the agent's fix differs.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import attrs
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ..utils.sandbox_state import get_sandbox_backend

# Tunable settings live in config.py (OPE-31); wiring constants stay here.
from .config import (
    COMMAND_TIMEOUT_SECONDS,
    DIFF_TIMEOUT_SECONDS,
    OUTPUT_TAIL_CHARS,
    WORKSPACE,
)

logger = logging.getLogger(__name__)
_TIMEOUT_EXIT_CODE = 124  # docker exec timeout convention (see docker_sandbox)
_PRECONDITION_EXIT_CODE = 127  # shell "command not found"
_PRECONDITION_MARKERS = (
    "command not found",
    "No such file or directory",
    "npm: not found",
    "uv: not found",
)


@attrs.define(frozen=True)
class GateCommand:
    """One CI-equivalent validation command for a project.

    ``paths`` holds optional project-relative glob filters (``**`` crosses
    directories); empty means the command always runs for that project.
    """

    name: str
    command: str
    paths: tuple[str, ...] = ()


# Ported verbatim from warehouse root workflow.md `projects.<name>.quality_gates`
# (copied 2026-07-28). Keys are warehouse monorepo root directories — the first
# path segment of a changed file selects the project.
PROJECT_QUALITY_GATES: dict[str, tuple[GateCommand, ...]] = {
    # workflow.md `projects.docdock.quality_gates`
    "docdock": (
        GateCommand("install backend dependencies", "cd backend && uv sync --locked --all-groups"),
        GateCommand("backend ruff check", "cd backend && uv run ruff check ."),
        GateCommand("backend ruff format check", "cd backend && uv run ruff format --check ."),
        GateCommand("backend mypy", "cd backend && uv run mypy ."),
        GateCommand("backend pytest", "cd backend && uv run pytest"),
        GateCommand("install frontend dependencies", "cd frontend && npm ci"),
        GateCommand("frontend eslint", "cd frontend && npm run lint"),
        GateCommand("frontend prettier check", "cd frontend && npm run format:check"),
    ),
    # workflow.md `projects.baydoor.quality_gates`
    "baydoor": (
        GateCommand("install dependencies", "npm ci"),
        GateCommand("eslint", "npm run lint"),
        GateCommand("test", "npm run test"),
        GateCommand("typecheck", "npm run typecheck"),
        # Keep these non-secret placeholders in sync with warehouse .github/workflows/ci.yml.
        GateCommand(
            "build",
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_Y2xlcmsuZXhhbXBsZS5jb20k "
            "DATABRICKS_CLIENT_ID=ci-placeholder "
            "LAKEBASE_ENDPOINT=projects/ci/branches/main/endpoints/placeholder "
            "PGDATABASE=ci_placeholder PGHOST=localhost npm run --if-present build",
        ),
        GateCommand(
            "frontend render check",
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_Y2xlcmsuZXhhbXBsZS5jb20k "
            "DATABRICKS_CLIENT_ID=ci-placeholder "
            "LAKEBASE_ENDPOINT=projects/ci/branches/main/endpoints/placeholder "
            "PGDATABASE=ci_placeholder PGHOST=localhost npm run render:check",
            paths=(
                "app/**",
                "components/**",
                "lib/**",
                "middleware.ts",
                "next.config.ts",
                "app/globals.css",
                "package.json",
                "package-lock.json",
            ),
        ),
    ),
    # workflow.md `projects.baypress.quality_gates`
    "baypress": (
        GateCommand(
            "validate Elementor Pro Composer auth secret",
            'test -n "$ELEMENTOR_PRO_LICENSE_KEY"',
        ),
        GateCommand(
            "install Composer dependencies from composer.lock",
            "export COMPOSER_AUTH=\"$(python3 -c 'import json, os; "
            'print(json.dumps({"http-basic": {"composer.elementor.com": '
            '{"username": "token", "password": '
            'os.environ["ELEMENTOR_PRO_LICENSE_KEY"].strip()}}}))\')" '
            "&& composer install --no-interaction --no-progress --prefer-dist",
        ),
        GateCommand("composer lint", "composer lint"),
        GateCommand(
            "frontend render check",
            "npm run render-check",
            paths=(
                "public/wp-content/themes/baypress-child/**",
                "public/wp-content/mu-plugins/**",
                "e2e/**",
            ),
        ),
    ),
    # workflow.md `projects.azure_infra.quality_gates` — offline/static only by
    # policy (FRG-489): never authenticate to or plan against live Azure.
    "azure_infra": (
        GateCommand(
            "keyvault terraform validate",
            "cd dev/keyvault && terraform init -backend=false -input=false"
            " && terraform validate -no-color",
        ),
        GateCommand(
            "blob terraform validate",
            "cd dev/blob && terraform init -backend=false -input=false"
            " && terraform validate -no-color",
        ),
        GateCommand(
            "lakebase terraform validate",
            "cd dev/lakebase && terraform init -backend=false -input=false"
            " && terraform validate -no-color",
        ),
    ),
    # workflow.md `projects.sb-sharepoint.quality_gates`
    "sb-sharepoint": (
        GateCommand("sharepoint ruff check", "uvx --from ruff==0.15.11 ruff check dupe_files"),
        GateCommand(
            "sharepoint ruff format check",
            "uvx --from ruff==0.15.11 ruff format --check dupe_files",
        ),
        GateCommand(
            "sharepoint mypy",
            "uvx --from mypy==1.20.2 mypy --config-file pyproject.toml dupe_files",
        ),
        GateCommand(
            "sharepoint pytest",
            "uv run --no-project --with pytest pytest -c pyproject.toml dupe_files/tests",
        ),
    ),
    # workflow.md `projects.speedbase.quality_gates`
    "speedbase": (
        GateCommand("lakebase ruff check", "uvx --from ruff==0.15.11 ruff check lakebase"),
        GateCommand(
            "lakebase ruff format check",
            "uvx --from ruff==0.15.11 ruff format --check lakebase",
        ),
        GateCommand(
            "lakebase mypy",
            "uvx --from mypy==1.20.2 mypy --config-file pyproject.toml lakebase",
        ),
        GateCommand(
            "lakebase pytest",
            "uv run --no-project --with pytest --with sqlalchemy --with psycopg2-binary"
            " --with alembic --with python-dotenv pytest -c pyproject.toml lakebase/tests",
        ),
    ),
}


@attrs.define(frozen=True)
class GateFailure:
    """Evidence for one failed quality-gate command.

    ``kind`` is ``"failure"`` (the command ran and failed), ``"precondition"``
    (tool/dependency missing — the agent's fix is environmental, not code), or
    ``"timeout"`` (killed at the per-command limit).
    """

    project: str
    command_name: str
    command: str
    exit_code: int | None
    kind: str
    output_tail: str


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a workflow.md-style glob (``**`` crosses ``/``) to a regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_glob_to_regex(p).match(path) for p in patterns)


def touched_projects(changed_paths: Sequence[str]) -> dict[str, list[str]]:
    """Map configured project roots to their project-relative changed paths.

    Paths whose first segment is not a configured project are ignored here;
    the caller logs them as a pass-with-notice (never silently invent
    commands for unconfigured projects).
    """
    projects: dict[str, list[str]] = {}
    for path in changed_paths:
        root, sep, rest = path.partition("/")
        if sep and root in PROJECT_QUALITY_GATES:
            projects.setdefault(root, []).append(rest)
    return projects


def _classify_failure(exit_code: int | None, output: str) -> str:
    if exit_code == _TIMEOUT_EXIT_CODE:
        return "timeout"
    if exit_code == _PRECONDITION_EXIT_CODE:
        return "precondition"
    if any(marker in output for marker in _PRECONDITION_MARKERS):
        return "precondition"
    return "failure"


async def run_quality_gates(
    backend: Any, changed_paths: Sequence[str], repo_dir: str
) -> GateFailure | None:
    """Run every selected project's gate commands; return the first failure.

    ``backend`` satisfies the deepagents async sandbox contract
    (``await backend.aexecute(command, timeout=...) -> ExecuteResponse`` —
    the thread-offloading async surface every ``BaseSandbox`` provides).
    Commands run from the project root under ``repo_dir`` — the resolved repo
    clone (see ``resolve_repo_dir``), not ``/workspace`` itself: the agent
    clones into ``/workspace/<repo>``, and ``PROJECT_QUALITY_GATES`` keys are
    directories inside that clone (OPE-59). Gate commands with ``paths``
    filters run only when a project-relative changed path matches.
    """
    selected = touched_projects(changed_paths)
    unconfigured = {
        path.partition("/")[0]
        for path in changed_paths
        if path.partition("/")[0] not in PROJECT_QUALITY_GATES
    }
    if unconfigured:
        logger.info(
            "quality gates: no configured commands for %s — passing with notice",
            sorted(unconfigured),
        )
    for project, rel_paths in selected.items():
        for gate in PROJECT_QUALITY_GATES[project]:
            if gate.paths and not any(_matches_any(p, gate.paths) for p in rel_paths):
                continue
            # Capture the command's true tail command-side: the sandbox backend
            # truncates long output by keeping the head, which would drop the
            # final error lines the agent needs. `tail -c` bounds output before
            # the backend ever sees it; pipefail preserves the exit code.
            full = (
                f"cd {shlex.quote(f'{repo_dir}/{project}')} && set -o pipefail && "
                f"( {gate.command} ) 2>&1 | tail -c {OUTPUT_TAIL_CHARS}"
            )
            response = await backend.aexecute(full, timeout=COMMAND_TIMEOUT_SECONDS)
            exit_code = getattr(response, "exit_code", None)
            output = getattr(response, "output", "") or ""
            if exit_code == 0:
                continue
            return GateFailure(
                project=project,
                command_name=gate.name,
                command=gate.command,
                exit_code=exit_code,
                kind=_classify_failure(exit_code, output),
                output_tail=output[-OUTPUT_TAIL_CHARS:],
            )
    return None


async def resolve_repo_dir(backend: Any, configurable: dict[str, Any]) -> str | None:
    """Directory of the run's repo clone under ``WORKSPACE``, or None.

    The agent clones with ``gh repo clone <owner>/<repo>`` (prompt.py), which
    creates ``/workspace/<name>`` — ``/workspace`` itself is never a git repo,
    which is why gate diffs must run against this resolved directory (OPE-59).
    The declared repo travels in ``configurable["repo"]``, resolved from the
    triggering Linear issue/comment per the OPE-49 precedence, so the clone
    may be open-swe, warehouse, or any allowlisted repo. Resolution order:

    1. ``{WORKSPACE}/{configurable["repo"]["name"]}`` when the sandbox
       confirms it is a git worktree (``git -C <dir> rev-parse``).
    2. Discovery fallback: exactly one ``/workspace/*/.git`` → its parent.
    3. None — zero or multiple clones and no usable declaration; callers keep
       their fail-open pass but must log at error level (a silently disabled
       gate is exactly the outage this helper exists to prevent).

    The directory is shell-quoted here and by callers: the name originates in
    issue/comment text (model- and author-controlled).
    """
    repo = configurable.get("repo")
    name = repo.get("name") if isinstance(repo, dict) else None
    if isinstance(name, str) and name.strip():
        candidate = f"{WORKSPACE}/{name.strip()}"
        response = await backend.aexecute(
            f"git -C {shlex.quote(candidate)} rev-parse --is-inside-work-tree",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if getattr(response, "exit_code", None) == 0:
            return candidate
    response = await backend.aexecute(f"ls -d {WORKSPACE}/*/.git", timeout=DIFF_TIMEOUT_SECONDS)
    if getattr(response, "exit_code", None) == 0:
        entries = [
            line.strip()
            for line in (getattr(response, "output", "") or "").splitlines()
            if line.strip().endswith("/.git")
        ]
        if len(entries) == 1:
            return entries[0].removesuffix("/.git")
    return None


async def _changed_paths(backend: Any, base: str, head: str, repo_dir: str) -> list[str] | None:
    """Changed file paths of the requested PR head vs the PR base, or None.

    Runs inside ``repo_dir`` (the resolved clone — see ``resolve_repo_dir``).
    Diffs the requested ``head`` ref (not the sandbox's current checkout,
    which may have moved after the push). Tries ``origin/<base>`` first (the
    push target), then ``<base>``; None means the diff could not be computed
    (fail-open at the caller). Refs are shell-quoted: they come from
    model-controlled tool args, and an unquoted metacharacter ref (e.g.
    ``feature;true``) could yield exit 0 with no paths — skipping every gate.
    """
    for ref in (f"origin/{base}", base):
        response = await backend.aexecute(
            f"git -C {shlex.quote(repo_dir)} diff --name-only "
            f"{shlex.quote(ref)}...{shlex.quote(head)}",
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        if getattr(response, "exit_code", None) == 0:
            return [line.strip() for line in response.output.splitlines() if line.strip()]
    return None


def _tool_name(request: ToolCallRequest) -> str | None:
    tool_call = getattr(request, "tool_call", None)
    if isinstance(tool_call, Mapping):
        name = tool_call.get("name")
        return name if isinstance(name, str) else None
    return None


def _tool_args(request: ToolCallRequest) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", None)
    args = tool_call.get("args") if isinstance(tool_call, Mapping) else None
    return dict(args) if isinstance(args, Mapping) else {}


def _tool_call_id(request: ToolCallRequest) -> str | None:
    tool_call = getattr(request, "tool_call", None)
    if isinstance(tool_call, Mapping):
        value = tool_call.get("id")
        return value if isinstance(value, str) else None
    return None


def _blocked_tool_message(request: ToolCallRequest, failure: GateFailure) -> ToolMessage:
    if failure.kind == "precondition":
        advice = (
            "This is a PRECONDITION failure (missing tool or dependency), not a "
            "test/lint failure: install or provision the missing prerequisite "
            "in the sandbox, or surface the environment gap, then retry."
        )
    elif failure.kind == "timeout":
        advice = (
            "The command hit the per-command timeout. Investigate the hang or "
            "long-running step, fix it, and retry open_pull_request."
        )
    else:
        advice = (
            "Fix the failing check in your branch and retry open_pull_request. "
            "Do not bypass it with gh pr create or direct API calls."
        )
    content = {
        "status": "error",
        "error_type": "QualityGateFailed",
        "code": "quality_gate_failed",
        "recoverable_by_agent": True,
        "error": (
            f"Quality gate '{failure.command_name}' for project "
            f"'{failure.project}' failed (exit {failure.exit_code}, "
            f"{failure.kind}). {advice}"
        ),
        "project": failure.project,
        "failing_command_name": failure.command_name,
        "failing_command": failure.command,
        "exit_code": failure.exit_code,
        "failure_kind": failure.kind,
        "output_tail": failure.output_tail,
    }
    return ToolMessage(
        content=json.dumps(content),
        tool_call_id=_tool_call_id(request),
        status="error",
    )


class QualityGatesMiddleware(AgentMiddleware):
    """Run the touched projects' CI-equivalent gates before a PR opens.

    Async-only interception: ``open_pull_request`` is an async tool, so every
    call routes through ``awrap_tool_call``. The sync ``wrap_tool_call``
    default passthrough is acceptable because the gated tool cannot execute on
    the sync path.
    """

    state_schema = AgentState

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if _tool_name(request) != "open_pull_request":
            return await handler(request)
        blocked = await self._blocking_message(request)
        if blocked is not None:
            return blocked
        return await handler(request)

    async def _blocking_message(self, request: ToolCallRequest) -> ToolMessage | None:
        try:
            args = _tool_args(request)
            base = args.get("base")
            if not isinstance(base, str) or not base:
                base = "main"
            head = args.get("head")
            # `head` may be "owner:branch"; the sandbox only knows the branch.
            head = head.rpartition(":")[2] if isinstance(head, str) and head else ""
            head = head or "HEAD"
            configurable = get_config().get("configurable", {})
            thread_id = configurable.get("thread_id")
            if not thread_id:
                logger.warning("quality gates: no thread_id in run config — passing")
                return None
            backend = await get_sandbox_backend(str(thread_id))
            repo_dir = await resolve_repo_dir(backend, configurable)
            if repo_dir is None:
                logger.error(
                    "quality gates: no repo clone found under %s (declared: %r) — passing",
                    WORKSPACE,
                    (configurable.get("repo") or {}).get("name"),
                )
                return None
            changed = await _changed_paths(backend, base, head, repo_dir)
            if changed is None:
                logger.error(
                    "quality gates: could not diff %r against base %r — passing", repo_dir, base
                )
                return None
            failure = await run_quality_gates(backend, changed, repo_dir)
        except Exception:
            # ponytail: infrastructure fault in the gate itself must not
            # permanently block PR creation — log loudly and fail open.
            logger.exception("quality gates: gate infrastructure error — passing")
            return None
        if failure is None:
            return None
        logger.info(
            "quality gates: blocking open_pull_request — %s / %s (exit %s, %s)",
            failure.project,
            failure.command_name,
            failure.exit_code,
            failure.kind,
        )
        return _blocked_tool_message(request, failure)
