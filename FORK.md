# Speed Bay fork of Open SWE

Fork of [langchain-ai/open-swe](https://github.com/langchain-ai/open-swe) (MIT).
This repo is our **deployment repo**: Speed Bay customizations live in files
upstream does not touch, so pulling upstream improvements stays a clean merge.

This file is the **merge contract**: what must be re-verified after every
upstream merge. Running/operating the deployment lives in
[`OPERATIONS.md`](OPERATIONS.md); onboarding from zero — new teammate or new
operator machine — follows [`SETUP.md`](SETUP.md).

## Upstream sync

```bash
git fetch upstream
git merge upstream/main
```

Remotes: `origin` → `speedbay/open-swe`, `upstream` → `langchain-ai/open-swe`.

### Re-check after every merge

Our customizations are new files, but each must be **registered** in an
upstream-owned file. These registrations are the entire merge surface — verify
all survived, and re-add any a merge dropped (each is marked in-code with a
`SPEEDBAY REGISTRATION` comment):

| # | Registration | Location |
|---|---|---|
| 1 | `"docker"` entry for the Docker sandbox backend | the `SANDBOX_FACTORIES` dict in `agent/utils/sandbox.py` |
| 2 | `SpeedbayConventionsMiddleware`, `PRStandardsMiddleware`, and `QualityGatesMiddleware` in the `middleware=[...]` list | the list inside `get_agent()` in `agent/server.py`, plus their direct imports above |
| 3 | `docker` branch calling `validate_startup_config()` (OPE-7), which also schedules the boot-time verify-sweep cron ensure (OPE-53) | inside `validate_sandbox_startup_config()` in `agent/utils/sandbox.py`; cron-ensure logic lives in `agent/speedbay/verify_sweep_cron.py` |
| 4 | `strip_server_runtime(...)` on the factory config (OPE-15) | `traced_graph_factory` in `agent/utils/tracing.py` + `get_scheduler` in `agent/scheduler.py`; any NEW langgraph.json graph that bypasses `traced_graph_factory` must add the strip |
| 5 | `speedbay_verify_trigger.maybe_handle(...)` verify-transition hook (OPE-39) | inside `linear_webhook()` in `agent/webhooks/linear_routes.py`, immediately after JSON parsing and before the Comment-type filter, plus its import above; logic lives in `agent/speedbay/verify_trigger.py` |
| 6 | `task == "verify_sweep"` scheduler branch (OPE-42) | inside `_launch()` in `agent/scheduler.py`, after the `reconcile` branch; logic lives in `agent/speedbay/verify_sweep.py`. The hourly cron is ensured idempotently at boot (registration #3); `speedbay/ensure_verify_sweep_cron.py` is list/inspect only |
| 7 | `speedbay_linear_guard.is_duplicate_comment(...)` duplicate-delivery dedup (OPE-56) | inside `linear_webhook()` in `agent/webhooks/linear_routes.py`, immediately before background-task dispatch after validation and repo resolution; logic lives in `agent/speedbay/linear_guard.py` |
| 8 | `subscription_model(...)` subscription-OAuth block (OPE-60) | at the top of `make_model()` in `agent/utils/model.py`, after the retry/timeout defaults and **before** the `openai:` base_url default (the OAuth model pins its own base URL and raises on conflict), plus its import above; logic lives in `agent/speedbay/subscription_auth.py` |
| 8 | `gate_approval_router` import + `include_router` (OPE-10) | `create_app()` in `agent/api/app.py`; logic lives in `agent/speedbay/gate_approval.py` + `agent/speedbay/gate_approval_api.py` |
| 9 | `export` on `agentsRequest` (OPE-10) | `ui/src/features/agents/lib/api.ts` — one-word visibility change so the fork-added `ui/src/features/agents/lib/gateApproval.ts` reuses the upstream request client |
| 10 | `GateApprovalCard` render site (OPE-10) | import + one JSX element in `AgentThreadView.tsx`; component and hooks are fork-added files |
| 11 | `PendingGateApprovalsBanner` render site (OPE-10) | import + one JSX element in `AgentsHome.tsx`; component and hooks are fork-added files |
| 12 | Wrapped `request_pr_review` tool registration (OPE-100) | direct import in `agent/server.py`; source gate lives in `agent/speedbay/review_request_gate.py` |
| 13 | `model_settings_router` host-only API registration (OPE-134) | import + `include_router` in `agent/api/app.py`; commit operation lives in `agent/speedbay/model_settings.py` |

OPE-100 deliberately gates review requests by run source rather than comparing the PR
head with the run's working branch: the Slack/GitHub allowlist covers every current
human-request lane and denies every implementation-run lane. Add a self-review check
only if a Slack-triggered run ever reviews its own PR.

Identified by symbol, not `file:line` — the middleware list moved from :946 to :953 on the very first upstream merge.

Both are upstream's documented extension points (`docs/CUSTOMIZATION.md` §6),
which is why they merge cleanly.

Note: upstream's `validate_sandbox_startup_config()` (in `agent/utils/sandbox.py`)
validates **only** `SANDBOX_TYPE=langsmith`; our `docker` branch there (the third
registration, same file as #1) adds boot-time validation — daemon reachable and
image present — so a misconfigured Docker setup fails at startup, not first run.

## File placement rule

Speed Bay code goes in **new files** under paths upstream doesn't own
(e.g. `agent/integrations/docker.py`, `agent/middleware/<our_gate>.py`).
Never edit upstream logic in place — register, don't modify.

**Documented exception:** dependency pins require editing upstream
`pyproject.toml`. Keep such edits to a single minimal line and expect to
re-apply them on merge.

## Judgment standards delivery (no fork middleware)

Speed Bay's judgment-based engineering standards (code style, design-it-twice,
seams/testing, deletion test, accessibility) are **AGENTS.md curation in the
target repo**, not fork code. Warehouse keeps them in its root `AGENTS.md`
("Engineering standards (judgment)") and UI-scoped `baydoor/AGENTS.md` /
`baypress/AGENTS.md` (OPE-17). Delivery is the existing upstream machinery:
the implementer prompt mandates reading repo-root `AGENTS.md` after clone,
`SubdirAgentsReadMiddleware` (`agent/middleware/subdir_agents.py`) appends
applicable ancestor `AGENTS.md` files to `read_file` results, and
`agent/utils/agents_md.py` inlines root + directory-scoped files into reviewer
prompts. Do not add fork middleware to deliver conventions — put them in the
target repo's `AGENTS.md` at the scope where they apply.

## Upstream deviations (re-check after every merge)

The upstream-owned files below carry edits. Each is marked in-code with
`SPEEDBAY DEVIATION` / `SPEEDBAY REGISTRATION` comments.

| File | Edit | Why not elsewhere |
|---|---|---|
| `agent/server.py` | Imports + two entries in the `get_agent()` middleware list (conventions, quality gates), plus the wrapped `request_pr_review` tool import (OPE-100), plus `pull.rebase true` in `_configure_git_identity` (OPE-109) | Sanctioned registration point; no alternative seam. The rebase default rides the only sandbox-global git-config call, so branch syncs never create merge commits that linear-history rulesets reject. |
| `agent/utils/tracing.py` | Import + one line in `traced_graph_factory`: pass `strip_server_runtime(config)` to the wrapped factory (OPE-15) | The single chokepoint every traced langgraph.json entrypoint (agent, reviewer, analyzer, chat) routes through — stripping here keeps the four factory files byte-identical to upstream. Logic lives in `agent/speedbay/runtime_compat.py`. |
| `agent/scheduler.py` | Import + `strip_server_runtime(config or {})` at its single `.with_config(...)` site (OPE-15) | The only langgraph.json factory registered without `traced_graph_factory`, so it needs the strip locally. |
| `pyproject.toml` | One added floor: `langgraph-api>=0.11.1` (OPE-15) | Upstream pins `langgraph>=1.1.10` unbounded, so uv resolves langgraph 1.2.x against langgraph-api 0.10.3 — below Studio's required 0.11. Single minimal line; re-apply after upstream merges. |
| `agent/webhooks/linear_routes.py` | Import + two guard calls after the `botActor` check: drop comments authored by the runtime `LINEAR_API_KEY` (OPE-23) and comments outside this instance's `OPENSWE_TRIGGER_OWNER_EMAILS` scope (OPE-36) | Loop prevention and instance routing must run at webhook-processing time; there is no sanctioned seam in the route. Logic lives in `agent/speedbay/linear_guard.py`; the route carries only the calls. |
| `agent/utils/model.py` | Import + one marked block at the top of `make_model()`: cache-checked `subscription_model(...)` short-circuit for subscription OAuth (OPE-60) | The single chokepoint every graph's model construction routes through; the block must precede the `openai:` base_url default it overrides. Fail-open: with `SPEEDBAY_SUBSCRIPTION_AUTH` unset it returns `None` and the API-key path is unchanged. |
| `agent/webhooks/common.py` | Warning in `upsert_agent_thread_owner_metadata` when owner attribution resolves without a GitHub login (OPE-84) | This upstream-owned persistence chokepoint sees every source and the final resolved identity; re-check that the warning remains immediately after login resolution so unlistable threads are observable without changing metadata or callers. |
| `agent/utils/linear_team_repo_map.py` | Upstream's own workspace mapping replaced with an empty dict | Docs designate this file as deployer config. Our Linear team "Open SWE" collided with upstream's entry of the same name and routed to `langchain-ai/open-swe`, which the allowlist rejected. Empty mapping falls back to `DEFAULT_REPO_OWNER`/`DEFAULT_REPO_NAME` (`speedbay/warehouse`); per-comment `repo:owner/name` still overrides. |
| `agent/api/app.py` | Import + `include_router(model_settings_router)` (OPE-134) | The FastAPI composition seam is the only way to mount the org-owned host-only model-setting operation without putting it under the dashboard route. |
| `agent/dashboard/team_settings.py` | Wrap `upsert_team_settings` Store write in the shared OPE-134 lock | This dashboard writer replaces the same shared record as the org-owned read/merge/write model commit; serializing both prevents process-local lost updates. |

Deliberately **not** patched, to keep the merge surface small:

- `agent/prompt.py` — upstream's PR/commit format instructions conflict with
  warehouse's contract, but that file takes ~70 commits per 90 days. Overriding
  it via `SpeedbayConventionsMiddleware` costs nothing at merge time.
- `agent/utils/authorship.py` — attribution is stripped by the `commit-msg` hook
  rather than by editing the footer/trailer helpers.
