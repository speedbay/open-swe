# Speed Bay fork of Open SWE

Fork of [langchain-ai/open-swe](https://github.com/langchain-ai/open-swe) (MIT).
This repo is our **deployment repo**: Speed Bay customizations live in files
upstream does not touch, so pulling upstream improvements stays a clean merge.

Upstream `docs/INSTALLATION.md` remains authoritative for install steps. This
file is authoritative for **fork conventions only**. Onboarding from zero —
new teammate or new operator machine — follows the ordered checklist in
[`SETUP.md`](SETUP.md), which sequences the sections below.

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
| 3 | `docker` branch calling `validate_startup_config()` | inside `validate_sandbox_startup_config()` in `agent/utils/sandbox.py` |
| 4 | `strip_server_runtime(...)` on the factory config (OPE-15) | `traced_graph_factory` in `agent/utils/tracing.py` + `get_scheduler` in `agent/scheduler.py`; any NEW langgraph.json graph that bypasses `traced_graph_factory` must add the strip |

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

## Local boot (credential-free)

There is no `.env.example` upstream. Regenerate `.env` from
`docs/INSTALLATION.md` §6. Three values must deviate from that block for a
credential-free local boot:

| Var | Value | Why |
|---|---|---|
| `LANGCHAIN_TRACING_V2` | `"false"` or absent — **never `""`** | starlette casts it to bool; `""` raises `ValueError` before any app code runs |
| `SANDBOX_TYPE` | `"docker"` | default is `langsmith`, which is fail-closed without `DEFAULT_SANDBOX_SNAPSHOT_ID`. `docker` (OPE-7) runs each agent in a container from `openswe-sandbox:dev` — build it first (see `speedbay/docker/Dockerfile.sandbox`). `local` still exists but has **no isolation**; only for credential-free bring-up on a machine with no real keys |
| `DASHBOARD_BASE_URL` | `""` | any `http://localhost*` value turns on the local-dev LLM key check (`agent/utils/model.py:295`), which requires a key for the default model |

Both boot validators run in the FastAPI lifespan at `agent/api/app.py:24-25`.

Verify a boot:

```bash
langgraph dev
curl -s http://localhost:2024/health    # {"status":"healthy"}
```

`DASHBOARD_ALLOWED_ORIGINS` must include `https://smith.langchain.com` for
LangGraph Studio to connect. Empty does **not** work: `create_app()`
(`agent/api/app.py:44`) skips `CORSMiddleware` entirely when the list is empty,
so Studio's cross-origin preflights get no CORS headers; a non-matching value
fails the same way. Either failure surfaces in the browser only as "Failed to
fetch". Both dashboard vars come back together when the dashboard is
configured.

## Webhook tunnel

The backend runs on a laptop, so inbound webhooks arrive through a **named
Cloudflare Tunnel** on the `speedbay.com` zone. This is the canonical URL for
the GitHub App (OPE-3) and the Linear trigger (OPE-5):

| | |
|---|---|
| Public base URL | `https://openswe.speedbay.com` |
| GitHub webhook path | `https://openswe.speedbay.com/webhooks/github` |
| Tunnel name / id | `openswe` / `66d09a43-7dac-4001-9adb-b6df1806796d` |
| Local target | `http://localhost:2024` |

Start it alongside `langgraph dev` (separate terminal):

```bash
cloudflared tunnel run --url http://localhost:2024 openswe
```

Verify the public path end to end — `cf-ray` proves the request traversed
Cloudflare rather than looping back through localhost:

```bash
curl -s -D- https://openswe.speedbay.com/health | grep -i 'HTTP/\|cf-ray'
```

A stopped tunnel returns Cloudflare `530`. Restarting reuses the same hostname
and tunnel id with no reconfiguration, so the GitHub/Linear webhook settings
never need editing.

### Per-machine setup

`cloudflared tunnel login` writes `~/.cloudflared/cert.pem`, and
`tunnel create` writes `~/.cloudflared/<UUID>.json`. **Both are secrets and
neither is committed.** A second dev runs `cloudflared tunnel login` against the
`speedbay.com` zone and either reuses this tunnel (copy its credentials file via
a password manager) or creates their own with a distinct hostname.

If a freshly created hostname fails to resolve locally while working fine from
`dig @1.1.1.1`, the local resolver cached an NXDOMAIN from a pre-creation
lookup: `sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder`.

### Unattended operation

With the Docker backend (OPE-7) live, agent commands run in per-run containers:
no host filesystem, host env, or host credentials are reachable (negative-proof
test in `tests/speedbay/test_docker_sandbox.py`). The blanket prohibition on
`cloudflared service install` is therefore lifted **conditionally**: an
always-on tunnel is acceptable only while `SANDBOX_TYPE=docker` — revert to
start/stop-per-session if the backend is ever switched back to `local`. What
still runs on the host regardless: the backend process itself, webhook
handling, and token minting. Keep those in mind before leaving the machine
unattended for long periods; the remaining laptop-phase risks are cost
(bounded by FORK.md § Spend limits) and PR volume, both bounded per-run by
human review before merge.

### Why Cloudflare rather than ngrok

Hostname control on a zone we already own (`openswe.speedbay.com`), no
interstitial page, and no request cap. (An earlier version of this section
justified the choice by event volume — ~35k webhook events/month — but that
figure assumed a `check_run` subscription that OPE-3 establishes we must not
create; the real figure is ~5k/month across the six handled events, which
ngrok's free tier could also absorb. The decision stands on the qualitative
grounds above, not on cap headroom.)

## Speed Bay org layer

Everything we add lives in files upstream does not own:

| Path | Purpose |
|---|---|
| `speedbay/mint_token.py` | Mints a GitHub App installation token on demand |
| `speedbay/bin/gh` | `gh` shim; replaces the hardcoded `GH_TOKEN=dummy` with a real token |
| `speedbay/bin/git-credential-openswe` | Git credential helper for `github.com` |
| `speedbay/gitconfig` | Registers the credential helper; sets the bot git identity |
| `speedbay/githooks/commit-msg` | Strips AI-attribution trailers from every commit |
| `speedbay/run-dev.sh` | Backend launcher (env + shims); invoked by `openswe start` |
| `speedbay/openswe` | **The lifecycle command**: `start` / `stop` / `status` — see § Operating |
| `speedbay/set_model.py` | Reads/sets the agent's default model (no dashboard needed) |
| `speedbay/create_linear_webhook.py` | Creates/lists the Linear trigger webhooks (needs a temp admin key) |
| `speedbay/docker/Dockerfile.sandbox` | The sandbox image (`openswe-sandbox:dev`) the docker backend boots |
| `agent/speedbay/` | **All Speed Bay agent modules**: `config.py` (OPE-31 — the single home for tunable org-layer settings: sandbox image/TTL/memory/timeouts, gate workspace + timeouts, corrective-round bound, Linear endpoint; env-overridable knobs resolve at call time), `conventions.py` (commit/PR contract middleware), `linear_guard.py` (OPE-23 self-trigger guard), `docker_sandbox.py` (OPE-7 docker backend), `quality_gates.py` (OPE-9 pre-PR quality gates; commands ported from warehouse workflow.md — re-sync on change), `pr_standards.py` (OPE-8 atomicity-cap + commit-hygiene gate on `open_pull_request`, incl. shell-fallback interception; OPE-34 forces gate-passing PRs to open ready-for-review, never draft), `runtime_compat.py` (OPE-15 — strips langgraph-api's `__pregel_runtime` from factory-bound configs), `rules/` (OPE-14 atomicity-cap + commit-hygiene rules as pure functions; consumed by `pr_standards.py`) |
| `tests/speedbay/` | **All Speed Bay tests** + fixtures: conventions, linear guard, docker sandbox (incl. the negative isolation proof; self-skips without a docker daemon/image/App key) |
| `.macroscope/` | Macroscope review config (OPE-26): blocking agent-hygiene CRA, org-layer Python correctness idioms, review-scope ignore file — see below |
| `.github/workflows/speedbay-ci.yml` | Org-layer CI: ruff + pytest over Speed Bay code only — see below |

### CI (org-layer only)

`speedbay-ci.yml` lints (`ruff check` / `ruff format --check`) and tests only
Speed Bay-authored code. The org layer is three whole directories —
`speedbay/`, `agent/speedbay/`, `tests/speedbay/` — so new org files and tests
enter CI (and Macroscope idiom scope) automatically; there is no file list to
maintain. Upstream-owned files carrying marked deviations follow upstream's
style and are not linted by us. Upstream's suites are upstream's job; two upstream
workflows are disabled **at the repo level** (`gh workflow disable`, a repo
setting that survives upstream merges — no file edits, zero merge surface):

- **Agent CI** (`ci.yml`) — whole-tree lint/format/tests/Playwright; its tests
  assert upstream behavior our deviations intentionally changed (kimi-k3
  rename, emptied team-repo map), so it can never pass here unmodified.
- **PR Title Lint** (`pr_lint.yml`) — requires conventional-commit titles,
  mutually exclusive with the `OPE-NNN:` titles the agent-hygiene check
  enforces.

Re-enable either with `gh workflow enable <name>`. `OpenWiki Update` and
`Promote main to prod` remain `disabled_fork` (GitHub's fork default).

### Macroscope review config (`.macroscope/`)

The `.macroscope/` directory is org-layer: upstream has no such directory, so
it carries **zero upstream merge surface**. It configures Macroscope's PR
review for this fork:

- `check-run-agents/agent-hygiene.md` — blocking (`conclusion: failure`) check
  enforcing the commit/PR hygiene contract (issue-prefixed titles,
  `Closes OPE-NNN`, four-section bodies, no AI attribution). Self-contained:
  the rules are inlined, with an explicit exemption for upstream-authored
  commits on upstream-sync PRs.
- `correctness/python-idioms.md` — Speed Bay Python house style, folded into
  the built-in Correctness review and scoped to org-layer paths only, so
  upstream Python is never judged against our idioms.
- `ignore.md` — Macroscope's default ignore patterns (REPLACE semantics
  require copying them) plus fork scoping: upstream bulk we never author
  (`ui/**`, generated `openwiki/**`) is out of review scope; upstream files we
  may carry deviations in (`agent/**`, `tests/**`) stay in scope.
- **Approvability: off; findings gate via required thread resolution
  (OPE-29, supersedes the OPE-26 omission).** Macroscope never approves
  PRs — the Approvability toggle is off in Macroscope's repo settings and
  there is no `.macroscope/approvability.md`. Instead, the `main` ruleset
  sets `required_review_thread_resolution`: every unresolved **inline review
  thread** — how Macroscope Correctness/CRA findings and human line comments
  land — must be resolved before merge. (Top-level issue comments and review
  summaries without inline threads do not gate.) (The built-in Correctness check always concludes
  `neutral`, even for HIGH findings — verified on PR #8 — so its *presence*
  is required in the ruleset but blocking comes from thread resolution.)
  Escape hatch: the ruleset's repo-admin `pull_request` bypass.

### Why the local sandbox needs all this

The agent never holds a GitHub token: upstream's prompts hardcode
`GH_TOKEN=dummy` and expect a **sandbox proxy** to swap in a real one. That proxy
is LangSmith-only — `refresh_proxy_token` in `agent/utils/github_proxy.py`
returns early unless `SANDBOX_TYPE=langsmith`, and imports
`_configure_github_proxy` from `agent/integrations/langsmith.py`. So with **any**
other backend (local, docker, e2b, daytona, modal) every `git`/`gh` call fails
with 401 out of the box.

`speedbay/run-dev.sh` replaces the proxy with host-side credentials. The `local`
backend runs commands with the parent process's environment
(`LocalShellBackend(..., inherit_env=True)`), so exporting `PATH`,
`GIT_CONFIG_GLOBAL` and `core.hooksPath` is enough — no upstream code changes.

The Docker backend (`agent/speedbay/docker_sandbox.py`, OPE-7) solves this
differently — containers do not inherit host env, so at create/reconnect it
mints a token on the host and provisions the container with a token file
(`/opt/speedbay/token`, refreshed on reconnect since installation tokens last
an hour), a `gh` shim at `/usr/local/bin/gh`, a read-only gitconfig whose
credential helper reads the token file, and the attribution-stripping
`commit-msg` hook. The App private key never enters the container.

### Bot-token-only mode requires a LangSmith placeholder

Counter-intuitively, running **without** LangSmith requires setting
`LANGSMITH_API_KEY_PROD`. `is_bot_token_only_mode()` in `agent/utils/auth.py` is
`LANGSMITH_API_KEY and not X_SERVICE_AUTH_JWT_SECRET and not USER_ID_API_KEY_MAP`;
with the key empty the code takes the per-user OAuth path instead and dies with
`No ls_user_id found from email ...`. The variable is fork-local (no installed
package reads it) and on the bot path is used only as a flag, so a placeholder
value is enough. `LANGSMITH_ENDPOINT_PROD` / `LANGSMITH_URL_PROD` point at
`http://127.0.0.1:9` so the other five call sites that would build a real
LangSmith client fail locally instead of reaching the network.

### Choosing the model

`LLM_MODEL_ID` in `.env` does **not** select the runtime model — it is read only
by `validate_local_dev_llm_config` as a boot-time credential check. The real
precedence is per-thread override -> user profile -> **team default**, stored in
the LangGraph Store (`team_settings` / `default`). Use:

```bash
speedbay/set_model.py                                            # show current
speedbay/set_model.py --list                                     # options
speedbay/set_model.py fireworks:accounts/fireworks/models/kimi-k3
```

Settings live in the Store, so they survive restarts but not a Store wipe.

## Upstream deviations (re-check after every merge)

The upstream-owned files below carry edits. Each is marked in-code with
`SPEEDBAY DEVIATION` / `SPEEDBAY REGISTRATION` comments.

| File | Edit | Why not elsewhere |
|---|---|---|
| `agent/server.py` | Imports + two entries in the `get_agent()` middleware list (conventions, quality gates) | Sanctioned registration point; no alternative seam |
| `agent/utils/tracing.py` | Import + one line in `traced_graph_factory`: pass `strip_server_runtime(config)` to the wrapped factory (OPE-15) | The single chokepoint every traced langgraph.json entrypoint (agent, reviewer, analyzer, chat) routes through — stripping here keeps the four factory files byte-identical to upstream. Logic lives in `agent/speedbay/runtime_compat.py`. |
| `agent/scheduler.py` | Import + `strip_server_runtime(config or {})` at its single `.with_config(...)` site (OPE-15) | The only langgraph.json factory registered without `traced_graph_factory`, so it needs the strip locally. |
| `pyproject.toml` | One added floor: `langgraph-api>=0.11.1` (OPE-15) | Upstream pins `langgraph>=1.1.10` unbounded, so uv resolves langgraph 1.2.x against langgraph-api 0.10.3 — below Studio's required 0.11. Single minimal line; re-apply after upstream merges. |
| `agent/dashboard/options.py` | `kimi-k3-code` -> `kimi-k3` in `SUPPORTED_MODELS` and `DEPRECATED_MODEL_REPLACEMENTS` | Upstream ships a model id that does not exist on Fireworks (404 from the platform API). `SUPPORTED_MODEL_IDS` gates model selection, so it cannot be fixed from config. **File upstream so this deviation disappears.** |
| `agent/webhooks/linear_routes.py` | Import + two guard calls after the `botActor` check: drop comments authored by the runtime `LINEAR_API_KEY` (OPE-23) and comments outside this instance's `OPENSWE_TRIGGER_OWNER_EMAILS` scope (OPE-36) | Loop prevention and instance routing must run at webhook-processing time; there is no sanctioned seam in the route. Logic lives in `agent/speedbay/linear_guard.py`; the route carries only the calls. |
| `agent/utils/linear_team_repo_map.py` | Upstream's own workspace mapping replaced with an empty dict | Docs designate this file as deployer config. Our Linear team "Open SWE" collided with upstream's entry of the same name and routed to `langchain-ai/open-swe`, which the allowlist rejected. Empty mapping falls back to `DEFAULT_REPO_OWNER`/`DEFAULT_REPO_NAME` (`speedbay/warehouse`); per-comment `repo:owner/name` still overrides. |

Deliberately **not** patched, to keep the merge surface small:

- `agent/prompt.py` — upstream's PR/commit format instructions conflict with
  warehouse's contract, but that file takes ~70 commits per 90 days. Overriding
  it via `SpeedbayConventionsMiddleware` costs nothing at merge time.
- `agent/utils/authorship.py` — attribution is stripped by the `commit-msg` hook
  rather than by editing the footer/trailer helpers.

## Linear trigger

A `@openswe` comment on a Linear ticket triggers a run. Setup facts that cost a
night to learn:

- **Linear's webhook UI no longer accepts a user-supplied secret** — it
  generates a `lin_wh_...` value. Upstream's docs (and this fork's `.env`
  layout) assume we choose the secret, so webhooks are created **via the API**
  with `speedbay/create_linear_webhook.py`, passing the `LINEAR_WEBHOOK_SECRET`
  from `.env`. A UI-created webhook's generated secret never verified against
  our HMAC check; the API-created webhook with our own secret verified
  immediately.
- **Only workspace admins can manage webhooks.** The swe-service-bot runtime key gets
  `Invalid role: admin required`. Use a temporary admin key
  (`LINEAR_ADMIN_KEY` env var), then revoke it. Never store it.
- **`allPublicTeams: true` does not cover private teams.** Each private Linear
  team needs its own webhook (`--team KEY`). Six webhooks currently exist: one
  for all public teams plus one per private team (OPE, STA, YAR, BPRESS,
  UDLINT), all sharing the same secret and URL. A newly created private team
  needs its own webhook — run the script again.
- **API-authored comments do fire the webhook**, and arrive with
  `botActor: null` — the route's bot filter does not catch comments posted with
  a plain API key (e.g. swe-service-bot). Since OPE-23 a deterministic guard closes
  this: the route drops any comment authored by the runtime `LINEAR_API_KEY`'s
  own user id (`agent/speedbay/linear_guard.py`; fail-open on Linear
  outages so humans are never blocked, retried per delivery). Consequence for
  testing: swe-service-bot comments can no longer trigger runs — use a personal
  account or the synthetic-delivery scripts. The conventions middleware also
  instructs the agent never to write `@openswe` in Linear comments
  (belt-and-braces).
- **Multi-laptop deployments scope triggers per instance (OPE-36).** Linear
  webhooks fan out to every registered URL, so with one stack per dev a
  single mention would start duplicate runs. Each instance sets
  `OPENSWE_TRIGGER_OWNER_EMAILS="<owner-email>"` and drops comments authored
  by anyone else (case-insensitive, comma-separated list; fail-closed when a
  scoped payload has no author email). Unset means unscoped — the
  single-instance deployment keeps accepting every human mention.
- The runtime `LINEAR_API_KEY` is a swe-service-bot service-account key: agent
  comments on tickets are attributed to `swe-service-bot@speedbay.com`, and the key
  is revocable without touching anyone's personal access.
- **Sandbox runs can mutate `speedbay/gitconfig`**: an agent once ran
  `gh auth setup-git`, which rewrote the credential helper in the file
  `GIT_CONFIG_GLOBAL` points at (routing pushes through the host's gh auth).
  The file is kept read-only (`chmod 444`) to fail such attempts loudly; if it
  shows up modified, restore it from git.

## Operating

```bash
speedbay/openswe start    # backend + tunnel, health-verified; refuses duplicates
speedbay/openswe status   # processes, local/public health, live sandbox containers
speedbay/openswe stop     # kills both; confirms the public URL went offline (530)
```

`start` fails fast when the docker daemon is down, when either health check
doesn't reach 200, or when an instance is already running. Logs:
`/tmp/openswe-backend.log`, `/tmp/openswe-tunnel.log`. Once `start` reports
LIVE, every `@openswe` Linear comment triggers a containerized run.

## Dashboard (OPE-11)

The in-tree dashboard is the forge-dash replacement — we write no custom UI.
Backend must be LIVE (above); then:

```bash
corepack enable pnpm                  # once per machine (pnpm ships via Node's corepack)
cd ui && pnpm install && pnpm dev     # http://localhost:3000 (Vite, port in ui/package.json)
```

### Login env checklist (INSTALLATION.md §8 §“For the dashboard login to
succeed” + §Troubleshooting)

The sample `.env` ships several of these **empty with an inline comment**
(e.g. `DASHBOARD_JWT_SECRET="" # Generate with: ...`) — each one produces a
different, unlabeled failure. Complete ALL of them before the first login:

| Var (file) | Local-dev value | Empty → symptom |
|---|---|---|
| `VITE_DASHBOARD_API_BASE_URL` (`ui/.env`) | `http://localhost:2024` | Login button 404s inside the SPA on :3000 |
| `GITHUB_APP_CLIENT_ID` / `GITHUB_APP_CLIENT_SECRET` (`.env`) | from the GitHub App (§3c) | `auth/login` 500 “not configured” |
| `DASHBOARD_JWT_SECRET` (`.env`) | `openssl rand -hex 32` | `auth/login` 500 |
| `DASHBOARD_API_BASE_URL` (`.env`) | `http://localhost:2024` (http ⇒ SameSite=Lax cookie) | callback/cookie failures |
| `DASHBOARD_BASE_URL` (`.env`) | `http://localhost:3000` | wrong post-login redirect |
| `DASHBOARD_ALLOWED_ORIGINS` (`.env`) | `http://localhost:3000` | CORS off — API calls from the UI fail |
| `TOKEN_ENCRYPTION_KEY` (`.env`) | a **Fernet** key: `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` | callback 400 `{"detail":"unknown error"}` (`EncryptionKeyMissingError` in the log) |
| `CONFIGURED_ADMINS` (`.env`) | comma-separated GitHub logins/emails | admin pages (user mappings, enabled repos) locked |
| `ALLOWED_GITHUB_ORGS` (`.env`) | `speedbay` | org gate disabled (fail-open) — **any** GitHub account can log in and read threads |

> Docs say `openssl rand -base64 32` for `TOKEN_ENCRYPTION_KEY`, but the code
> feeds it straight to `Fernet(...)`, which requires **url-safe** base64 —
> openssl output containing `+`/`/` is rejected. Use the Fernet generator.

The GitHub App must also list `http://localhost:2024/dashboard/api/auth/callback`
as a callback URL (§3b) — App settings, not env. Restart both processes
(`speedbay/openswe stop && speedbay/openswe start`; re-run `pnpm dev`) after changing either
env file. When a login fails with a generic JSON error, the real exception is
in `/tmp/openswe-backend.log`.

### User mappings without Slack OAuth

Admin → User mappings is **remove-only**; mappings are normally created when a
user connects Slack from Profile Settings. This deployment has no `SLACK_*`
env configured, so that path is disabled — seed mappings directly into the
LangGraph Store instead (same record shape the Slack flow writes), then
restart the backend so its in-process mapping cache reloads:

```python
# .venv/bin/python — one record per dev
from langgraph_sdk import get_client
await get_client(url="http://localhost:2024").store.put_item(
    ["user_mappings"], "<github-login>",
    {"github_login": "<github-login>", "work_email": "<email>",
     "slack_user_id": None, "source": "slack_oauth", "status": "active",
     "created_at": "<iso-now>", "updated_at": "<iso-now>"})
```

Login is the direct GitHub OAuth (`GITHUB_APP_CLIENT_ID/SECRET`), gated by
`ALLOWED_GITHUB_ORGS="speedbay"` — non-org accounts are rejected server-side,
fail-closed (`tests/dashboard/test_dashboard_org_login_gate.py`,
`test_public_repo_org_gate.py`).

### forge-dash → dashboard mapping

| forge-dash job (warehouse) | Dashboard surface |
|---|---|
| Run queue + issue overview (attention/running/awaiting-merge/queued groups) | Agents → thread/run history (per-thread status; no issue-centric grouping — see gaps) |
| Per-run detail: scorecard, gate evidence, issue snapshot | Thread view: messages, plan, linked PR; approvals page holds gate state (OPE-10) |
| Actions: restart run, resume gate, reconcile | Follow-up message in the thread; approvals page approve/reject; a rerun is a new `@openswe` trigger |
| Daily spend + cost caps (cost-tracker) | Per-agent usage/cost (`agent_usage`) + fork spend limits (OPE-18) |
| Daemon operational snapshot / dispatch queue | n/a — no daemon here; runs are webhook-triggered (accepted gap) |
| Linear state snapshot | Linear itself; threads link their triggering issue/PR |
| — (never had) | Per-user **model profiles** (cheap Fireworks vs Claude routing), Admin: enabled repos + user mappings, review styles, automations |

### Gaps (input to the pilot verdict, not a build order)

- No issue-centric "needs attention" grouping with reasons (gate/failed/stalled/ci-failing); triage happens per-thread.
- No run scorecard equivalent — review evidence lives in Macroscope, gate state in the approvals page.
- No transactional restart with path selection; re-trigger via Linear comment instead.

## Spend limits (OPE-18)

**Open SWE enforces no per-run cost cap of its own.** Nothing in the fork stops
a looping or thrashing run from spending until it finishes or is killed by
hand. The provider-console limits below are the only backstop; they were sized
for the laptop phase (2026-07-28) and must be revisited before the OPE-12
pilot's sustained spend.

| Provider | Billing model | Hard cap | Alerts | Owner |
|---|---|---|---|---|
| Fireworks (primary — all runs bill here) | Prepaid credits, **auto-reload off** | Balance itself (~$56 at setup) | $50 / $100 / $1k / $10k spend | cbass |
| OpenAI | Monthly limit, hard-enforced, auto-refresh off | $200/mo | 80% and 100% | cbass |
| Anthropic | Monthly limit | $1,000/mo | email at $500 | cbass |
| Google, Exa | Keys declared in `.env` but **empty** — no accounts in use; dependent tools fail cleanly | n/a | n/a | — |

Why the OpenAI/Anthropic caps matter even though runs bill Fireworks only:
the team default lives in the LangGraph Store, and if the Store is wiped the
model silently reverts to `DEFAULT_MODEL_ID` (`openai:gpt-5.6-sol`), which also
enables the OpenAI↔Anthropic fallback pair. The caps bound that config
accident, not normal operation.

### What a hit cap looks like at runtime

`fireworks:` primaries get **no fallback and no retry middleware**
(`fallback_model_id_for` returns `None` for non-OpenAI/Anthropic providers, and
`agent/server.py` only installs `ModelFallbackMiddleware` when a fallback
exists) — so a provider error surfaces directly and ends the run rather than
retrying forever.

Auth-failure signature (captured live with a bogus key):

```
HTTP 401
{"error": {"message": "The API key you provided is invalid.",
           "code": "UNAUTHORIZED", "type": "error"}}
```

An exhausted prepaid balance is expected to return an HTTP 4xx from the same
endpoint with a quota/billing message rather than `UNAUTHORIZED`. Not yet
observed live — when it first happens, paste the actual body here. Diagnosis
rule: a run that dies immediately at its first model call with a 4xx from
`api.fireworks.ai` is a **billing/cap event, not a code fault** — check the
Fireworks balance before debugging anything.

## Fireworks Zero Data Retention (OPE-37)

Company policy requires strict ZDR for all prompt/generation data sent to
Fireworks. Per the official policy
(<https://docs.fireworks.ai/guides/security_compliance/data_handling>), ZDR is
the **default** — prompts/generations live only in volatile memory — but only
on the ZDR surface: **open models over the Chat Completions API**. The three
documented ways out, and how this fork closes each:

| ZDR escape hatch | Our posture |
|---|---|
| Responses API with `store=True` (its default; retains conversations 30 days) | Fireworks models build as `langchain_fireworks.ChatFireworks` → Chat Completions only. The Responses-API path in `agent/utils/model.py` (`use_responses_api`/`store`) is gated on `openai:` ids and defaults `store=False` even there. |
| Per-feature opt-in logging (FireOptimizer, fine-tuning datasets, batch files) | Not used. Console audit 2026-07-30 (evidence on OPE-37): Datasets, Fine-Tuning, and Batch API all empty. No self-serve "share data" toggle exists — opt-in logging is enterprise-contact-only, and ToS says content sharing is off by default. |
| Proprietary Fireworks models (e.g. FireFunction — logs I/O 30 days) | Only open models are selectable. `SUPPORTED_MODELS` in `agent/dashboard/options.py` **is** the allowlist; profile, team-settings, and `set_model.py` writes all validate against `SUPPORTED_MODEL_IDS`. |

Guardrail: `tests/models/test_fireworks_zdr.py` pins the ZDR-verified
Fireworks ids and fails if an unvetted `fireworks:` id is added or if
Fireworks kwargs ever carry `store`/`use_responses_api`. **Adding a Fireworks
model** therefore has one extra step: confirm it is an open model, then extend
`ZDR_VERIFIED_FIREWORKS_IDS` in that test.

Caveat: with `LANGSMITH_GATEWAY_ENABLED`, calls proxy through the LangSmith
LLM Gateway, which traces every call — Fireworks-side ZDR still holds, but
LangSmith retention is a separate vendor question outside this section.

## Known issues

- **Studio graph preview 500s** — `langgraph-api` 0.10.3 substitutes
  `langgraph_sdk.runtime._ReadRuntime`, which has no `override()`; `langgraph`
  1.2.8 calls it (`langgraph/pregel/_algo.py:691`). Upgrade path:
  `langgraph-api>=0.11.1`. `_ExecutionRuntime` lacks `override()` too, so real
  agent runs may hit this as well — unverified.
