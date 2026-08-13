from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FORK = ROOT / "FORK.md"
INSTALLATION = ROOT / "docs/INSTALLATION.md"
MARKER = "<!-- SPEEDBAY DEVIATION (OPE-136): source-checked graph inventory; see FORK.md -->"


def _middleware() -> set[str]:
    tree = ast.parse((ROOT / "agent/server.py").read_text())
    imports = {
        name.asname or name.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level
        and (node.module or "").startswith("speedbay.")
        for name in node.names
        if name.name.endswith("Middleware")
    }
    agent = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_agent"
    )
    call = next(
        node
        for node in ast.walk(agent)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_deep_agent"
        and any(keyword.arg == "middleware" for keyword in node.keywords)
    )
    value = next(keyword.value for keyword in call.keywords if keyword.arg == "middleware")
    middleware = value.args[-1] if isinstance(value, ast.Call) else value
    assert isinstance(middleware, ast.List)
    return {
        item.func.id
        for item in middleware.elts
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id in imports
    }


def _row(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(prefix))


def _assert_middleware(text: str, expected: set[str]) -> None:
    for prefix in "| 2 |", "| `agent/server.py` |":
        assert set(re.findall(r"`(\w+Middleware)`", _row(text, prefix))) == expected


def _assert_graphs(text: str, expected: dict[str, str]) -> None:
    table = text.split(MARKER, 1)[1].strip().split("\n\n", 1)[0]
    inventory = {
        match.group(1): match.group(2).strip()
        for line in table.splitlines()[2:]
        if (match := re.fullmatch(r"\| `(\w+)` \| (.+) \|", line))
    }
    assert set(inventory) == set(expected)
    assert all(inventory.values())
    section = text.split("The `langgraph.json` at the project root", 1)[1]
    assert json.loads(section.split("```json", 1)[1].split("```", 1)[0])["graphs"] == expected
    assert "three graphs" not in text.lower()


def test_fork_middleware_inventory_is_registered() -> None:
    _assert_middleware(FORK.read_text(), _middleware())


def test_fork_middleware_omissions_fail_contract() -> None:
    source, text, omitted = _middleware(), FORK.read_text(), next(iter(_middleware()))
    for prefix in "| 2 |", "| `agent/server.py` |":
        row = _row(text, prefix)
        with pytest.raises(AssertionError):
            _assert_middleware(text.replace(row, row.replace(f"`{omitted}`", ""), 1), source)


def test_installation_graph_inventory_is_registered() -> None:
    _assert_graphs(
        INSTALLATION.read_text(), json.loads((ROOT / "langgraph.json").read_text())["graphs"]
    )


def test_installation_graph_omission_fails_contract() -> None:
    source, text = (
        json.loads((ROOT / "langgraph.json").read_text())["graphs"],
        INSTALLATION.read_text(),
    )
    with pytest.raises(AssertionError):
        _assert_graphs(text.replace(f"| `{next(iter(source))}` |", "| |", 1), source)
