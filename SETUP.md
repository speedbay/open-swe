# Open SWE setup — zero to running

This checklist has two paths:

- **Path A — new teammate.** Join the existing always-on deployment and start
  triggering runs. No local clone, tunnel, webhook, or running process is
  required.
- **Path B — host administration (operators).** Provision, operate, and upgrade
  the shared deployment on the Azure host.

Every phase ends with a **Verify** command or observable result. Operational
background and troubleshooting live in [`OPERATIONS.md`](OPERATIONS.md);
upstream GitHub App and environment mechanics live in
[`docs/INSTALLATION.md`](docs/INSTALLATION.md).

---

## Path A — new teammate on the existing deployment

1. **GitHub:** get added to the `speedbay` GitHub organization (dashboard login
   is org-gated, fail-closed) and confirm you can see
   `github.com/speedbay/warehouse`.
2. **Linear:** get a seat in the `linear.app/speed-bay` workspace with access to
   your team's project.
3. **User mapping:** ask a host administrator to seed your
   `github-login → work-email` mapping. The administrator runs this on the
   Azure host, replacing the example login and email:

   ```bash
   cd /home/openswe/open-swe
   GITHUB_LOGIN="octocat" WORK_EMAIL="teammate@speedbay.com" .venv/bin/python - <<'PY'
   import asyncio
   import os
   from datetime import UTC, datetime

   from langgraph_sdk import get_client

   async def main():
       now = datetime.now(UTC).isoformat()
       login = os.environ["GITHUB_LOGIN"].strip().lower()
       email = os.environ["WORK_EMAIL"]
       client = get_client(url="http://127.0.0.1:2024")
       await client.store.put_item(
           ["user_mappings"],
           login,
           {
               "github_login": login,
               "work_email": email,
               "slack_user_id": None,
               "source": "slack_oauth",
               "status": "active",
               "created_at": now,
               "updated_at": now,
           },
       )
       print(login, "->", email, "active")

   asyncio.run(main())
   PY
   sudo systemctl restart openswe-backend.service
   ```

   Expected: one `active` line for the new teammate. This direct Store mapping
   is needed while Slack OAuth is disabled; see OPERATIONS.md § User mappings
   without Slack OAuth. Unmapped users can trigger runs, but with limited bot
   permissions and no attribution.
4. **Trigger a run:** comment `@openswe <what you want>` on a Linear ticket from
   your personal account. Bot/API comments are dropped by the self-trigger
   guard (OPERATIONS.md § Linear trigger). The run works the ticket's repository
   and returns PRs attributed to you.
5. **Watch it:** open <https://openswe-dash.speedbay.com>, sign in with GitHub,
   and confirm the run appears in the sidebar thread list. Usage appears under
   `/usage`.

**Verify:** the thread appears within seconds, the run completes, its PR opens
ready for review, and its usage is visible in the dashboard.

---

## Path B — Host administration (operators)

All commands in this path run on the Azure host unless stated otherwise. The
checked-in deployment definitions are in [`speedbay/deploy/`](speedbay/deploy/).
Laptop-hosted and multi-machine deployments remain supported for development;
use OPERATIONS.md § Webhook tunnel → Per-machine setup and § Linear trigger
rather than adding their tunnels, webhooks, or owner scopes to member
onboarding.

### B1. Host prerequisites

- Ubuntu 24.04 host with the `openswe` account and `/home/openswe` home
- Docker daemon, `git`, [`uv`](https://docs.astral.sh/uv/), Node 22+,
  `cloudflared`, and Caddy
- pnpm enabled once through Node's Corepack: `corepack enable pnpm`
- A read-only `speedbay/open-swe` deploy key registered in GitHub and available
  through the operations password manager
- Speed Bay GitHub App credentials and Cloudflare named-tunnel credentials
  available through the operations password manager

Grant the dedicated service account Docker access so the backend and prune units
can use the daemon:

```bash
sudo usermod -aG docker openswe
```

Provision an 8 GiB swapfile as a pressure buffer and persist it across reboots:

```bash
if [ "$(stat -f -c %T /)" = btrfs ]; then
    sudo touch /swapfile
    sudo chattr +C /swapfile
fi
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Verify:**

```bash
sudo -u openswe docker info >/dev/null && git --version && uv --version && node -v
pnpm --version && cloudflared --version && caddy version
```

Verify the swapfile separately:

```bash
swapon --show
```

Install the read-only deploy key for the `openswe` account, replacing the source
placeholder below, then verify that it can read the private repository:

```bash
sudo install -d -o openswe -g openswe -m 700 /home/openswe/.ssh
sudo install -o openswe -g openswe -m 600 /path/to/open-swe-deploy-key /home/openswe/.ssh/id_ed25519
sudo -Hu openswe sh -c 'ssh-keyscan -H github.com > ~/.ssh/known_hosts'
sudo -u openswe git ls-remote git@github.com:speedbay/open-swe.git HEAD
```

Install the named-tunnel credential from the operations password manager at the
exact path referenced by `speedbay/deploy/tunnel-config.yml`, replacing the
source placeholder below:

```bash
sudo install -d -o openswe -g openswe -m 700 /home/openswe/.cloudflared
sudo install -o openswe -g openswe -m 600 /path/to/66d09a43-7dac-4001-9adb-b6df1806796d.json /home/openswe/.cloudflared/66d09a43-7dac-4001-9adb-b6df1806796d.json
sudo -u openswe test -r /home/openswe/.cloudflared/66d09a43-7dac-4001-9adb-b6df1806796d.json
```

### B2. Clone and populate host environment

The systemd units use this exact checkout path:

```bash
sudo -Hu openswe git clone git@github.com:speedbay/open-swe.git /home/openswe/open-swe
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && uv sync'
```

Populate `/home/openswe/open-swe/.env` from `docs/INSTALLATION.md` §6, then
apply these OPERATIONS.md checklists:

1. § Local boot (credential-free): set `LANGCHAIN_TRACING_V2="false"` and
   `SANDBOX_TYPE="docker"`. Do not use that section's local dashboard URL
   value on the production host.
2. § Dashboard → Login env checklist: populate
   `GITHUB_APP_CLIENT_ID/SECRET`, `DASHBOARD_JWT_SECRET`,
   `TOKEN_ENCRYPTION_KEY`, `CONFIGURED_ADMINS`, and
   `ALLOWED_GITHUB_ORGS="speedbay"`.
3. § Dashboard → Production (host) values: set `DASHBOARD_API_BASE_URL`,
   `DASHBOARD_BASE_URL`, `DASHBOARD_ALLOWED_ORIGINS`, and
   `ui/.env`'s `VITE_DASHBOARD_API_BASE_URL` for
   `https://openswe-dash.speedbay.com`.

The shared instance is intentionally unscoped. Do not add any per-owner trigger
filter to the host environment. Optional feature variables can remain empty
when their features are disabled.

**Verify that required login variables are present and non-empty:**

```bash
.venv/bin/python - <<'PY'
from dotenv import dotenv_values

required = {
    "ALLOWED_GITHUB_ORGS",
    "CONFIGURED_ADMINS",
    "DASHBOARD_ALLOWED_ORIGINS",
    "DASHBOARD_API_BASE_URL",
    "DASHBOARD_BASE_URL",
    "DASHBOARD_JWT_SECRET",
    "GITHUB_APP_CLIENT_ID",
    "GITHUB_APP_CLIENT_SECRET",
    "TOKEN_ENCRYPTION_KEY",
}
values = dotenv_values(".env")
missing = sorted(key for key in required if not values.get(key))
if missing:
    raise SystemExit(f"missing values: {', '.join(missing)}")
print("env-ready")
PY
```

Expected: `env-ready`. Also confirm `ui/.env` contains the production
`VITE_DASHBOARD_API_BASE_URL` value.

### B3. Build the sandbox image

Run checkout mutations as the `openswe` deployment account; its Docker-group access
from B1 permits the image build without changing checkout ownership. The image bakes
warehouse-compatible Playwright Chromium into `/ms-playwright`; project setup (including
`npm ci`) does not establish browser availability, so run the browser-launch smoke too:

```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker && docker run --rm --entrypoint sh openswe-sandbox:dev -lc "rm -f /tmp/playwright-browser-smoke.png; playwright screenshot --browser chromium about:blank /tmp/playwright-browser-smoke.png && test -s /tmp/playwright-browser-smoke.png"'
```

Expected: the **Playwright Chromium browser-launch smoke** exits successfully
after writing a non-empty screenshot.

### B4. Build the dashboard

```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe/ui && pnpm install --frozen-lockfile && pnpm build'
```

**Verify:** `test -f ui/.output/public/_shell.html && echo dashboard-built` prints
`dashboard-built`.

### B5. Install and enable host services

Install the checked-in unit files as symlinks, then enable the backend, shared
tunnel, dashboard, prune timer, and thread-retention timer:

```bash
cd /home/openswe/open-swe
sudo ln -sfn "$PWD/speedbay/deploy/openswe-backend.service" /etc/systemd/system/openswe-backend.service
sudo ln -sfn "$PWD/speedbay/deploy/openswe-tunnel.service" /etc/systemd/system/openswe-tunnel.service
sudo ln -sfn "$PWD/speedbay/deploy/openswe-dashboard.service" /etc/systemd/system/openswe-dashboard.service
sudo ln -sfn "$PWD/speedbay/deploy/openswe-prune.service" /etc/systemd/system/openswe-prune.service
sudo ln -sfn "$PWD/speedbay/deploy/openswe-prune.timer" /etc/systemd/system/openswe-prune.timer
sudo ln -sfn "$PWD/speedbay/deploy/openswe-thread-retention.service" /etc/systemd/system/openswe-thread-retention.service
sudo ln -sfn "$PWD/speedbay/deploy/openswe-thread-retention.timer" /etc/systemd/system/openswe-thread-retention.timer
sudo systemctl daemon-reload
sudo systemctl enable --now openswe-backend.service openswe-tunnel.service openswe-dashboard.service openswe-prune.timer openswe-thread-retention.timer
```

Webhooks target the shared `https://openswe.speedbay.com` hostname; members do
not register their own. Only when a new **private** Linear team is created, run
this once from `/home/openswe/open-swe` with the new team's key and a temporary
workspace-admin key exported as `LINEAR_ADMIN_KEY`, then revoke the admin key:

```bash
LINEAR_TEAM_KEY="OPE"
speedbay/create_linear_webhook.py --create --team "$LINEAR_TEAM_KEY"
```

### B6. Operate systemd services

```bash
systemctl status openswe-backend.service openswe-tunnel.service openswe-dashboard.service
sudo systemctl restart openswe-backend.service openswe-tunnel.service openswe-dashboard.service
journalctl -u openswe-backend.service -u openswe-tunnel.service -u openswe-dashboard.service
speedbay/openswe status
```

Use `systemctl` for start, stop, restart, and boot supervision. Use
`speedbay/openswe status` only for deployment health and sandbox status; do not
mix its `start` or `stop` commands with systemd. Process output is in
`/tmp/openswe-backend.log` and `/tmp/openswe-tunnel.log`; supervisor and Caddy
output is in the journal.

### B7. Upgrade the deployment

```bash
sudo -Hu openswe sh -c 'cd /home/openswe/open-swe && git pull --ff-only && uv sync && docker build -f speedbay/docker/Dockerfile.sandbox -t openswe-sandbox:dev speedbay/docker && cd ui && pnpm install --frozen-lockfile && pnpm build'
sudo systemctl daemon-reload
sudo systemctl restart openswe-backend.service openswe-tunnel.service openswe-dashboard.service
```

Then run the B6 status command and confirm all three services are active.

### B8. Smoke run (end to end)

Pick a genuinely small docs-only Linear ticket, comment the trigger mention
from a personal account, and observe:

1. The thread appears at <https://openswe-dash.speedbay.com> within seconds.
2. The run completes and its PR opens ready for review with a
   `Closes <TICKET>` body that passes the hygiene gate.
3. Cost appears under <https://openswe-dash.speedbay.com/usage>.
4. On merge, Linear moves the ticket to `ready-for-verify`.

**Verify:** all four outcomes are observed.

---

## When something fails

| Symptom | First look |
|---|---|
| Dashboard login button returns 404 | `ui/.env` missing the production `VITE_DASHBOARD_API_BASE_URL`; rebuild with B4 |
| `auth/login` returns 500 | Empty checklist variable; inspect `journalctl -u openswe-backend.service` and `/tmp/openswe-backend.log` |
| Callback returns 400 `unknown error` | `TOKEN_ENCRYPTION_KEY` empty or invalid (`EncryptionKeyMissingError` in the backend log) |
| Public health is unavailable | `systemctl status openswe-backend.service openswe-tunnel.service`, then their journal and `/tmp` logs |
| Run never starts from a Linear comment | Bot/API author was dropped, or a newly created private team needs the one-time B5 webhook command |
| Dashboard is unavailable | `systemctl status openswe-dashboard.service` and `journalctl -u openswe-dashboard.service` |
| Anything else | `journalctl` for the three services, `/tmp/openswe-backend.log`, then OPERATIONS.md § Known issues |
