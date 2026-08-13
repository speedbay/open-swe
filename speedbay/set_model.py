#!/usr/bin/env python3
"""Get or set the agent's default model without the dashboard.

`LLM_MODEL_ID` in .env does NOT choose the runtime model — it is read only by
`validate_local_dev_llm_config` as a boot-time credential check. The real
precedence is per-thread override -> user profile -> team default, and the team
default lives in the LangGraph Store under namespace ["team_settings"], key
"default" (agent/dashboard/team_settings.py).

Usage:
    speedbay/set_model.py                       # show current settings
    speedbay/set_model.py <model_id> [effort]   # set agent + subagent default
    speedbay/set_model.py --list                # show selectable model ids + efforts
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.dashboard.options import SUPPORTED_MODELS  # noqa: E402

BASE = "http://localhost:2024"
NAMESPACE = ["team_settings"]
KEY = "default"


def _request(path: str, body: dict, method: str = "POST") -> dict | None:
    """Send JSON to the local backend and return its parsed response."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{path} failed: {exc.code} {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {BASE} — is speedbay/run-dev.sh running? ({exc})") from exc


def get_settings() -> dict:
    """Return stored team settings, or {} when nothing has been written yet."""
    found = _request("/store/items/search", {"namespace_prefix": NAMESPACE, "limit": 10}) or {}
    for item in found.get("items", []):
        if item.get("key") == KEY:
            return item.get("value") or {}
    return {}


def set_model(model_id: str, effort: str) -> dict:
    """Commit one agent/subagent pair through the host-only atomic operation."""
    response = _request(
        "/speedbay/model-settings/agent-default",
        {"model_id": model_id, "effort": effort},
        method="PUT",
    )
    if not isinstance(response, dict):
        raise SystemExit("model settings commit returned no response")
    return response


def _print_models(settings: dict) -> None:
    for field in sorted(settings):
        if "model" in field or "effort" in field:
            print(f"  {field:44} {settings[field]!r}")


def _effective_pair(response: dict, name: str) -> tuple[str, str]:
    pair = response.get(name)
    if not isinstance(pair, dict):
        raise SystemExit(f"model settings commit returned no effective {name} pair")
    model_id, effort = pair.get("model_id"), pair.get("effort")
    if not isinstance(model_id, str) or not isinstance(effort, str):
        raise SystemExit(f"model settings commit returned an invalid effective {name} pair")
    return model_id, effort


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--list":
        for m in SUPPORTED_MODELS:
            print(
                f"{m['id']:52} efforts: {', '.join(m['efforts'])} (default: {m['default_effort']})"
            )
        return
    if not args:
        settings = get_settings()
        if not settings:
            print(
                "no team settings stored — agent falls back to DEFAULT_MODEL_ID (openai:gpt-5.6-sol)"
            )
            return
        _print_models(settings)
        return

    model_id = args[0]
    option = next((m for m in SUPPORTED_MODELS if m["id"] == model_id), None)
    effort = args[1] if len(args) > 1 else (option["default_effort"] if option else "medium")
    response = set_model(model_id, effort)
    main_pair = _effective_pair(response, "main")
    subagent_pair = _effective_pair(response, "subagent")
    requested = (model_id, effort)
    if main_pair != requested or subagent_pair != requested:
        raise SystemExit(
            "model settings commit did not take effect: "
            f"main={main_pair!r}, subagent={subagent_pair!r}, requested={requested!r}"
        )
    print(f"effective main agent: {main_pair[0]} (effort={main_pair[1]})")
    print(f"effective subagent: {subagent_pair[0]} (effort={subagent_pair[1]})")


if __name__ == "__main__":
    main()
