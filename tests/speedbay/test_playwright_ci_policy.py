"""Static policy checks for the external Playwright compatibility workflow."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_WORKFLOW = ROOT / ".github/workflows/speedbay-dashboard-e2e.yml"
COMPATIBILITY_WORKFLOW = ROOT / ".github/workflows/speedbay-playwright-compatibility.yml"
PLAYWRIGHT_CONFIG = ROOT / "tests/e2e/playwright.config.ts"
CACHE_KEY = (
    "playwright-ui-${{ runner.os }}-node22-${{ hashFiles('ui/package.json', "
    "'ui/pnpm-lock.yaml', 'ui/vite.config.ts', 'ui/src/**', 'ui/public/**', "
    "'tests/e2e/global-setup.ts') }}"
)


def _block(text: str, heading: str, indent: int) -> str:
    prefix = " " * indent
    match = re.search(rf"(?m)^{re.escape(prefix + heading)}\s*$", text)
    assert match, f"missing {heading} block"
    start = match.end() + 1
    lines = text[start:].splitlines(keepends=True)
    end = 0
    for line in lines:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        end += len(line)
    return text[start : start + end]


def _list(block: str, heading: str, indent: int) -> list[str]:
    list_block = _block(block, heading, indent - 2)
    prefix = re.escape(" " * indent)
    return re.findall(rf'(?m)^{prefix}- ["\']?([^"\'\n]+)["\']?$', list_block)


def test_dashboard_workflow_retains_only_unit_coverage() -> None:
    workflow = DASHBOARD_WORKFLOW.read_text()
    jobs = _block(workflow, "jobs:", 0)

    assert re.search(r"(?m)^  dashboard-unit:$", jobs)
    assert not re.search(r"(?m)^  e2e:$", jobs)
    assert "playwright" not in workflow.lower()
    assert "name: Speedbay Dashboard" in workflow


def test_compatibility_workflow_has_exact_trigger_boundary() -> None:
    workflow = COMPATIBILITY_WORKFLOW.read_text()
    triggers = _block(workflow, "on:", 0)
    pull_request = _block(triggers, "pull_request:", 2)

    assert re.search(r'(?m)^  schedule:\n    - cron: "30 13 \* \* \*"$', triggers)
    assert re.search(r"(?m)^  workflow_dispatch:$", triggers)
    assert _list(pull_request, "paths:", 6) == [
        "tests/e2e/**",
        ".github/workflows/speedbay-playwright-compatibility.yml",
    ]
    assert not re.search(r"(?m)^  (push|pull_request_target|workflow_run):", triggers)


def test_compatibility_job_uses_optimized_complete_suite() -> None:
    workflow = COMPATIBILITY_WORKFLOW.read_text()
    cache_step = _block(workflow, "- name: Restore built dashboard", 6)
    artifact_step = _block(workflow, "- name: Upload Playwright report", 6)

    assert "permissions:\n  contents: read" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "setup-bun" not in workflow and re.search(r"(?i)\bbun\b", workflow) is None
    assert "uses: actions/cache@v4" in cache_step
    assert "path: ui/.output" in cache_step
    assert f"key: {CACHE_KEY}" in cache_step
    assert "restore-keys:" not in cache_step
    assert "run: npx playwright test --fail-on-flaky-tests" in workflow
    assert "uses: actions/upload-artifact@v7" in artifact_step
    assert "retention-days: 7" in artifact_step


def test_playwright_records_retry_only_diagnostics_and_fails_flakes() -> None:
    config = PLAYWRIGHT_CONFIG.read_text()

    assert "failOnFlakyTests: !!process.env.CI" in config
    assert 'trace: "on-first-retry"' in config
    assert 'video: "on-first-retry"' in config
    assert 'screenshot: "only-on-failure"' in config
    assert "workers: 1" in config
