# Open SWE setup — zero to running

Ordered onboarding checklist. Every step is either an exact command or a
pointer to the authoritative doc section (OPERATIONS.md here; upstream
`docs/INSTALLATION.md` for GitHub App / LangSmith mechanics). Each phase ends
with a **Verify** command and its expected output — run them; do not assume.

Two paths:

- **Path A — new teammate.** The deployment already runs on an operator's
  machine. You need trigger rights and visibility. ~10 minutes, no code.
- **Path B — operator machine.** Stand up the whole stack from a clone.

---

## Path A — new teammate on the existing deployment

1. **GitHub**: get added to the `speedbay` GitHub org (dashboard login is
   org-gated, fail-closed) and confirm you can see
   `github.com/speedbay/warehouse`.
2. **Linear**: get a seat in the `linear.app/speed-bay` workspace with access
   to your team's project.
3. **User mapping**: an operator seeds your `github-login → work-email`
   mapping (OPERATIONS.md § User mappings without Slack OAuth — the UI cannot
   create mappings until Slack OAuth is configured). Unmapped users can still
   trigger runs, but with limited bot permissions and no attribution.
4. **Trigger a run**: comment `@openswe <what you want>` on a Linear ticket
   from your personal account (bot/API comments are dropped by the
   self-trigger guard, OPERATIONS.md § Linear trigger). The run works the ticket's
   repo; PRs come back attributed to you.
5. **Watch it**: on the operator machine's dashboard (`http://localhost:3000`),
   your run appears in the sidebar thread list; cost lands under `/usage`.
   Remote dashboard access is a known gap — the GitHub App callback is
   registered for the operator host only.

**Verify (operator runs, after step 3):**

```bash
cd open-swe && .venv/bin/python - <<'EOF'
import asyncio
from langgraph_sdk import get_client
async def main():
    r = await get_client(url="http://localhost:2024").store.search_items(["user_mappings"], limit=50)
    items = r["items"] if isinstance(r, dict) else r.items
    for it in items:
        v = it["value"] if isinstance(it, dict) else it.value
        print(v["github_login"], "->", v["work_email"], v["status"])
asyncio.run(main())
EOF
```

Expected: one `active` line per mapped teammate, including the new one.

---

## Path B — operator machine from zero

### B1. Prerequisites

- Docker daemon (or Colima) running: `docker info` succeeds
- [`uv`](https://docs.astral.sh/uv/), Node 22+ (`corepack enable pnpm` once),
  `gh` CLI authenticated as **you**, `cloudflared`
- The Speed Bay GitHub App already exists (create one only for a brand-new
  org: `docs/INSTALLATION.md` §3, incl. **Organization → Members: Read-only**
  permission and both OAuth callback URLs from §3b/3c)

**Verify:** `docker info >/dev/null && uv --version && node -v && gh auth status && cloudflared --version`

### B2. Clone and env

```bash
git clone https://github.com/speedbay/open-swe && cd open-swe
uv sync                                    # creates .venv
```

Populate `.env` from `docs/INSTALLATION.md` §6, then apply — in this order —
the two OPERATIONS.md checklists, which exist because the sample block ships
several values **empty with an inline comment**, each failing differently:

1. OPERATIONS.md § Local boot (credential-free) — the three deviations
   (`LANGCHAIN_TRACING_V2`, `SANDBOX_TYPE="docker"`, dashboard vars).
2. OPERATIONS.md § Dashboard → **Login env checklist** — the full required table:
   `GITHUB_APP_CLIENT_ID/SECRET`, `DASHBOARD_JWT_SECRET`,
   `DASHBOARD_API_BASE_URL`, `DASHBOARD_BASE_URL`,
   `DASHBOARD_ALLOWED_ORIGINS`, `ALLOWED_GITHUB_ORGS` (empty = org gate
   **off**, fail-open), `TOKEN_ENCRYPTION_KEY` (**Fernet** key — see the
   checklist's caveat; the docs' openssl command intermittently produces
   invalid keys), `CONFIGURED_ADMINS`, and `ui/.env` with
   `VITE_DASHBOARD_API_BASE_URL`.

**Verify (no empty login-path vars):**

```bash
awk -F= '/^[A-Z][A-Z0-9_]*=/{v=$2; sub(/[ \t]*#.*$/,"",v); gsub(/"/,"",v); if (length(v)==0) print $1}' .env
```

Expected: **none of the B2 checklist vars appear.** Optional-feature vars may
remain empty (SLACK_*, EXA_API_KEY, GOOGLE_API_KEY, LangSmith project ids,
DEFAULT_SANDBOX_* snapshot vars, PUBLIC_REPO_ORG_GATE, and similar — all gate
features this deployment doesn't use).

### B3. Sandbox image

```bash
docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker
```

**Verify:** `docker image inspect openswe-sandbox:dev >/dev/null && echo image-present`

### B4. Tunnel (per machine)

OPERATIONS.md § Webhook tunnel → Per-machine setup: `cloudflared tunnel login`
against the `speedbay.com` zone, then create your **own hostname** (e.g.
`openswe-<you>.speedbay.com`).

> **Multi-operator rule (OPE-36).** With one stack per laptop, every Linear
> webhook fires to every registered URL. Before registering your webhook
> (B5), set `OPENSWE_TRIGGER_OWNER_EMAILS="<your-work-email>"` in `.env` —
> your instance then acts only on *your* `@openswe` comments, so one mention
> triggers exactly one backend. Every operator instance must be scoped; an
> unscoped instance accepts everyone's triggers and reintroduces duplicates.
> Trade-off: if your laptop is asleep, your trigger doesn't run — no other
> instance picks it up.

**Verify:** `ls ~/.cloudflared/cert.pem ~/.cloudflared/*.json`

### B5. Linear webhooks (once per hostname)

The existing webhooks point at the first operator's hostname. Each additional
operator registers their own set (same teams, **their** hostname) with
`speedbay/create_linear_webhook.py` and a temporary admin key (OPERATIONS.md
§ Linear trigger — UI-created webhooks never verify; API-created ones with
our shared `LINEAR_WEBHOOK_SECRET` do). Only do this **after** B4's owner
scope is set.

**Verify:** covered by the end-to-end smoke run in B8 — and confirm a
teammate's mention does *not* start a run on your instance (log line:
"comment author outside this instance's owner scope").

### B6. Boot

```bash
speedbay/openswe start     # backend + tunnel; fails fast on docker/image/health problems
```

Runs `langgraph dev` with `--no-reload` (the watcher otherwise reloads on the
runtime's own `.langgraph_api/` persistence flushes until it dies). After any
merged code change: `speedbay/openswe stop && speedbay/openswe start`.

**Verify:** `speedbay/openswe status` → backend + tunnel PIDs, both health
checks `200`.

### B7. Dashboard

```bash
cd ui && pnpm install && pnpm dev &        # http://localhost:3000
```

Log in via GitHub (OPERATIONS.md § Dashboard). Set your defaults under
**Open SWE Agent** settings (model, default repository; leave Base Branch and
Branch Prefix empty — a prefix breaks the issue-keyed branch convention the
PR-standards gate enforces). Seed teammate mappings (Path A step 3).

**Verify:** `curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:2024/dashboard/api/auth/login?redirect_to=%2Fagents"` → `302`
(500 means an env var from B2 is missing — the real error is in
`/tmp/openswe-backend.log`), and `http://localhost:3000/login` completes the
GitHub round-trip to `/agents`.

### B8. Smoke run (end to end)

Pick a genuinely small, docs-only ticket in Linear (or file one), comment the
trigger mention from your personal account, and watch:

1. Thread appears in the dashboard sidebar within seconds.
2. Run completes; PR opens **ready-for-review** with a `Closes <TICKET>` body
   that passes the hygiene gate.
3. Cost appears at `http://localhost:3000/usage`.
4. On merge, Linear moves the ticket to `ready-for-verify`.

**Verify:** all four observed. That is the deployment working with full
reliability; anything less, start at `/tmp/openswe-backend.log`.

---

## When something fails

| Symptom | First look |
|---|---|
| Login button 404s on :3000 | `ui/.env` missing `VITE_DASHBOARD_API_BASE_URL` |
| `auth/login` 500 | empty checklist var — exact mapping in OPERATIONS.md § Login env checklist |
| Callback 400 `unknown error` | `TOKEN_ENCRYPTION_KEY` empty/invalid (`EncryptionKeyMissingError` in the log) |
| `ERR_CONNECTION_REFUSED` on :2024 | backend down — `speedbay/openswe status`, then the log |
| Run never starts from a Linear comment | posted by a bot/API key (guard drops it), or webhook missing for a new private team (B5) |
| Anything else | `/tmp/openswe-backend.log`, then OPERATIONS.md § Known issues |
