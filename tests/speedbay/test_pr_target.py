from __future__ import annotations

import asyncio
from typing import Any

import attrs
import pytest

from agent.speedbay import pr_target as pt


@attrs.define(frozen=True)
class Response:
    output: str = ""
    exit_code: int = 0


class Backend:
    def __init__(self, origin: str = "https://github.com/acme/widget.git") -> None:
        self.origin = origin
        self.commands: list[str] = []

    async def aexecute(self, command: str, **_: Any) -> Response:
        self.commands.append(command)
        if "remote get-url" in command:
            return Response(self.origin)
        if "rev-parse" in command:
            return Response("h" * 40)
        return Response()


class GitHub:
    def __init__(self, branches: dict[str, str], pr: dict[str, Any] | None = None) -> None:
        self.branches = branches
        self.pr = pr
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str) -> Any:
        class Reply:
            def __init__(self, status_code: int, value: Any) -> None:
                self.status_code, self.value = status_code, value

            def json(self) -> Any:
                return self.value

        if "/pulls/" in url:
            return Reply(200, self.pr)
        branch = url.rsplit("/", 1)[-1]
        sha = self.branches.get(branch)
        return Reply(200 if sha else 404, {"commit": {"sha": sha}})

    async def patch(self, url: str, *, json: dict[str, Any]) -> None:
        self.patches.append((url, json))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def args(**override: str) -> dict[str, str]:
    return {
        "owner": "acme",
        "repo": "widget",
        "base": "main",
        "head": "forker:feature",
        "title": "OPE-121: bind target",
        "body": "body",
        **override,
    }


def test_resolve_target_preserves_fork_owner_and_sha() -> None:
    backend = Backend()
    github = GitHub({"main": "b" * 40, "feature": "h" * 40})
    target = run(
        pt.resolve_target(
            args(),
            {"repo": {"owner": "acme", "name": "widget"}},
            backend,
            "/workspace/widget",
            body="prepared body",
            client=github,
        )
    )
    assert target.head == "forker:feature"
    assert target.head_sha == "h" * 40
    assert target.body == "prepared body"
    assert any(
        "https://github.com/forker/widget.git feature" in command for command in backend.commands
    )


@pytest.mark.parametrize(
    ("tool_owner", "origin"),
    [
        ("other", "https://github.com/acme/widget.git"),
        ("acme", "https://github.com/other/widget.git"),
    ],
)
def test_resolve_target_rejects_cross_repository_identity(tool_owner: str, origin: str) -> None:
    with pytest.raises(pt.PRTargetError):
        run(
            pt.resolve_target(
                args(owner=tool_owner),
                {"repo": {"owner": "acme", "name": "widget"}},
                Backend(origin),
                "/workspace/widget",
                body="body",
                client=GitHub({"main": "b" * 40, "feature": "h" * 40}),
            )
        )


def test_revalidate_refuses_moved_head() -> None:
    target = pt.PRTarget(
        "acme", "widget", "main", "b" * 40, "forker", "feature", "h" * 40, "t", "b", "/r"
    )
    with pytest.raises(pt.PRTargetError, match="moved"):
        run(pt.revalidate_head(target, GitHub({"feature": "n" * 40})))


def test_verify_result_closes_only_new_mismatched_pr() -> None:
    target = pt.PRTarget(
        "acme", "widget", "main", "b" * 40, "forker", "feature", "h" * 40, "t", "b", "/r"
    )
    github = GitHub({}, {"title": "wrong"})
    result = run(pt.verify_result({"success": True, "created": True, "number": 7}, target, github))
    assert result["code"] == "pr_target_mismatch"
    assert github.patches == [
        ("https://api.github.com/repos/acme/widget/pulls/7", {"state": "closed"})
    ]
