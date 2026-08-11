"""Immutable GitHub PR target shared by the org-owned gate path."""

from __future__ import annotations

import contextlib
import contextvars
import json
import shlex
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from langchain_core.messages import ToolMessage

from .config import DIFF_TIMEOUT_SECONDS

GITHUB_API = "https://api.github.com"
_TARGET: contextvars.ContextVar[PRTarget | None] = contextvars.ContextVar("pr_target", default=None)


class PRTargetError(ValueError):
    """A requested PR target cannot be proven safe to gate or create."""


@dataclass(frozen=True)
class PRTarget:
    owner: str
    repo: str
    base: str
    base_sha: str
    head_owner: str
    head_branch: str
    head_sha: str
    title: str
    body: str
    repo_dir: str

    @property
    def head(self) -> str:
        return (
            self.head_branch
            if self.head_owner == self.owner
            else f"{self.head_owner}:{self.head_branch}"
        )


def current_target() -> PRTarget | None:
    return _TARGET.get()


@contextlib.contextmanager
def target_scope(target: PRTarget) -> Any:
    token = _TARGET.set(target)
    try:
        yield target
    finally:
        _TARGET.reset(token)


def target_error_message(request: Any, error: Exception) -> ToolMessage:
    tool_call = getattr(request, "tool_call", {})
    call_id = tool_call.get("id") if isinstance(tool_call, Mapping) else None
    return ToolMessage(
        content=json.dumps(
            {
                "status": "error",
                "error_type": "PRTargetInvalid",
                "code": "pr_target_invalid",
                "recoverable_by_agent": False,
                "error": str(error),
            }
        ),
        tool_call_id=call_id if isinstance(call_id, str) else None,
        status="error",
    )


def _value(args: Mapping[str, Any], key: str, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise PRTargetError(f"open_pull_request requires a non-empty {key}")
    return value.strip()


def _repo_identity(configurable: Mapping[str, Any]) -> tuple[str, str]:
    routed = configurable.get("repo")
    if not isinstance(routed, Mapping):
        raise PRTargetError("The routed repository is missing; refusing to select a clone")
    owner, name = routed.get("owner"), routed.get("name")
    if (
        not isinstance(owner, str)
        or not owner.strip()
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise PRTargetError("The routed repository must include owner and name")
    return owner.strip(), name.strip()


def _head(owner: str, raw: str) -> tuple[str, str]:
    if raw.count(":") > 1:
        raise PRTargetError("PR head must be a branch or owner:branch")
    head_owner, separator, branch = raw.partition(":")
    if separator:
        if not head_owner or not branch:
            raise PRTargetError("PR head must use a non-empty owner:branch")
        return head_owner, branch
    return owner, raw


async def _github_branch(client: httpx.AsyncClient, owner: str, repo: str, branch: str) -> str:
    response = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/branches/{quote(branch, safe='')}"
    )
    if response.status_code != 200:
        raise PRTargetError(f"GitHub could not resolve {owner}/{repo}:{branch}")
    data = response.json()
    sha = data.get("commit", {}).get("sha") if isinstance(data, Mapping) else None
    if not isinstance(sha, str) or not sha:
        raise PRTargetError(f"GitHub returned no commit SHA for {owner}/{repo}:{branch}")
    return sha


async def _git(backend: Any, command: str) -> str:
    response = await backend.aexecute(command, timeout=DIFF_TIMEOUT_SECONDS)
    output = (getattr(response, "output", "") or "").strip()
    if getattr(response, "exit_code", None) != 0:
        raise PRTargetError(f"Sandbox could not verify PR target: {output or command}")
    return output


async def _verify_clone(backend: Any, repo_dir: str, owner: str, repo: str) -> None:
    origin = await _git(backend, f"git -C {shlex.quote(repo_dir)} remote get-url origin")
    normalized = origin.removesuffix(".git").rstrip("/").lower()
    if not normalized.endswith(f"/{owner}/{repo}".lower()):
        raise PRTargetError(
            "The sandbox clone origin does not match the routed destination repository"
        )


async def _fetch_commit(
    backend: Any, repo_dir: str, owner: str, repo: str, branch: str, sha: str
) -> None:
    quoted = shlex.quote(repo_dir)
    await _git(
        backend,
        f"git -C {quoted} fetch https://github.com/{owner}/{repo}.git {shlex.quote(branch)}",
    )
    resolved = await _git(backend, f"git -C {quoted} rev-parse --verify FETCH_HEAD^{{commit}}")
    if resolved.splitlines()[0] != sha:
        raise PRTargetError(f"Sandbox fetched {owner}:{branch} at a different commit than GitHub")


async def resolve_target(
    args: Mapping[str, Any],
    configurable: Mapping[str, Any],
    backend: Any,
    repo_dir: str,
    *,
    body: str,
    client: httpx.AsyncClient,
) -> PRTarget:
    routed_owner, routed_repo = _repo_identity(configurable)
    owner, repo = _value(args, "owner"), _value(args, "repo")
    if (owner.lower(), repo.lower()) != (routed_owner.lower(), routed_repo.lower()):
        raise PRTargetError("Tool destination does not match the routed repository")
    base = _value(args, "base", "main")
    head_owner, head_branch = _head(owner, _value(args, "head"))
    await _verify_clone(backend, repo_dir, owner, repo)
    base_sha = await _github_branch(client, owner, repo, base)
    head_sha = await _github_branch(client, head_owner, repo, head_branch)
    await _fetch_commit(backend, repo_dir, head_owner, repo, head_branch, head_sha)
    return PRTarget(
        owner=owner,
        repo=repo,
        base=base,
        base_sha=base_sha,
        head_owner=head_owner,
        head_branch=head_branch,
        head_sha=head_sha,
        title=_value(args, "title"),
        body=body,
        repo_dir=repo_dir,
    )


def rewrite_request(request: Any, target: PRTarget) -> Any:
    tool_call = getattr(request, "tool_call", None)
    if not isinstance(tool_call, Mapping) or not isinstance(tool_call.get("args"), Mapping):
        return request
    args = {
        **tool_call["args"],
        "owner": target.owner,
        "repo": target.repo,
        "base": target.base,
        "head": target.head,
        "title": target.title,
        "body": target.body,
        "draft": False,
    }
    return request.override(tool_call={**tool_call, "args": args})


async def revalidate_head(target: PRTarget, client: httpx.AsyncClient) -> None:
    sha = await _github_branch(client, target.head_owner, target.repo, target.head_branch)
    if sha != target.head_sha:
        raise PRTargetError("PR head moved after validation; retry to build and gate a new target")


def _identity_matches(pr: Mapping[str, Any], target: PRTarget) -> bool:
    base = pr.get("base")
    head = pr.get("head")
    if not isinstance(base, Mapping) or not isinstance(head, Mapping):
        return False
    base_repo, head_repo = base.get("repo"), head.get("repo")
    if not isinstance(base_repo, Mapping) or not isinstance(head_repo, Mapping):
        return False
    return (
        base_repo.get("full_name", "").lower() == f"{target.owner}/{target.repo}".lower()
        and base.get("ref") == target.base
        and base.get("sha") == target.base_sha
        and head_repo.get("owner", {}).get("login", "").lower() == target.head_owner.lower()
        and head_repo.get("name", "").lower() == target.repo.lower()
        and head.get("ref") == target.head_branch
        and head.get("sha") == target.head_sha
        and pr.get("title") == target.title
        and pr.get("body") == target.body
    )


async def verify_result(result: Any, target: PRTarget, client: httpx.AsyncClient) -> Any:
    if not isinstance(result, Mapping) or not result.get("success"):
        return result
    number = result.get("number")
    if not isinstance(number, int):
        return {
            "success": False,
            "code": "pr_target_mismatch",
            "error": "GitHub returned no PR number",
        }
    response = await client.get(f"{GITHUB_API}/repos/{target.owner}/{target.repo}/pulls/{number}")
    details = response.json() if response.status_code == 200 else None
    if isinstance(details, Mapping) and _identity_matches(details, target):
        return result
    if result.get("created"):
        await client.patch(
            f"{GITHUB_API}/repos/{target.owner}/{target.repo}/pulls/{number}",
            json={"state": "closed"},
        )
    return {
        "success": False,
        "code": "pr_target_mismatch",
        "recoverable_by_agent": False,
        "error": "GitHub PR identity differs from the validated target",
    }


@contextlib.asynccontextmanager
async def github_client(token: str) -> AsyncIterator[httpx.AsyncClient]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        yield client
