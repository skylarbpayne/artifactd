# artifactd

`artifactd` is a small workspace runtime for agent-generated **Things**: HTML/JS/CSS artifacts that agents can create, protect, organize, share, and revisit.

It is meant to be boring infrastructure: a local FastAPI sidecar, SQLite metadata, static file serving, a profile/workspace password, expiring share links, tag filters, and a thin capability bridge back into the owning Hermes profile.

## Repository

```text
https://github.com/skylarbpayne/artifactd
```

The repo is currently private. To point another agent/person at it, give their GitHub account or deploy key read access first.

## What it does

- Deploy a single `.html` file or a directory with `index.html`.
- Serve artifacts from SQLite metadata + managed static files.
- Provide a protected **Workspace Home** at `/` for open/search/update/share/pin/archive flows.
- Store titles, descriptions, lifecycle status, pinning, expiration, capabilities, and flexible tags.
- Filter Things by tag(s), with text search composing with tag filters.
- Use one workspace/master password per profile by default.
- Save master-password UX client-side via `localStorage`; server stores password hashes only.
- Generate randomized expiring share links; default share TTL is 7 days.
- Expose safe server-side actions only through explicit capabilities + CSRF + audit logs.
- Install as a Hermes profile-local plugin wrapper without patching Hermes core.
- Expose over Tailscale Serve/Funnel or Cloudflare Tunnel.

## Architecture in one minute

```text
agent / Hermes profile
        │
        │ creates HTML + metadata
        ▼
artifactd CLI / plugin tool
        │
        ├── SQLite metadata: title, desc, tags, auth, share token hashes, capabilities
        ├── static files: sites/<slug>/index.html
        └── FastAPI server: Workspace Home, protected Things, share links, actions
                 │
                 ├── Tailscale Serve/Funnel, preferred for Safrin-style private deployments
                 └── Cloudflare Tunnel, used by Skylar/Palmer/Echo today
```

Generated Things are **not** Hermes plugins. `artifactd` is the installable sidecar/plugin package; Things are the fast generated surfaces it hosts.

## Install for an agent/profile

Prereqs:

- Python 3.9+
- GitHub read access to `skylarbpayne/artifactd`
- `git`
- optional but recommended: `tailscale` for private tailnet exposure

Clone and install:

```bash
git clone git@github.com:skylarbpayne/artifactd.git
cd artifactd

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'

pytest tests -q
artifactd --help
```

If SSH auth is not set up, use HTTPS instead:

```bash
git clone https://github.com/skylarbpayne/artifactd.git
```

## Quick start for Safrin

This creates an isolated Safrin workspace, installs the Hermes plugin wrapper, and prepares a local server on port `8789`.

```bash
cd artifactd
. .venv/bin/activate

PROFILE=safrin
PORT=8789
HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes}"
WORKSPACE_HOME="$HERMES_ROOT/profiles/$PROFILE/workspaces"

# Store this outside git. Do not hardcode shared passwords into the repo.
export ARTIFACTD_WORKSPACE_PASSWORD='<choose-or-provide-workspace-password>'

artifactd workspaces install \
  --profile "$PROFILE" \
  --hermes-root "$HERMES_ROOT" \
  --password "$ARTIFACTD_WORKSPACE_PASSWORD"

artifactd workspaces smoke \
  --profile "$PROFILE" \
  --hermes-root "$HERMES_ROOT" \
  --password "$ARTIFACTD_WORKSPACE_PASSWORD"

artifactd workspaces install-plugin \
  --profile "$PROFILE" \
  --hermes-root "$HERMES_ROOT" \
  --runtime-path "$(command -v artifactd)" \
  --port "$PORT" \
  --enable
```

Create a persistent cookie secret and start the local server:

```bash
mkdir -p "$WORKSPACE_HOME"
if [ ! -f "$WORKSPACE_HOME/.cookie-secret" ]; then
  python - <<'PY' > "$WORKSPACE_HOME/.cookie-secret"
import secrets
print(secrets.token_urlsafe(48))
PY
  chmod 600 "$WORKSPACE_HOME/.cookie-secret"
fi

ARTIFACTD_PROFILE="$PROFILE" \
ARTIFACTD_COOKIE_SECRET="$(cat "$WORKSPACE_HOME/.cookie-secret")" \
artifactd --home "$WORKSPACE_HOME" serve --profile "$PROFILE" --port "$PORT"
```

In production, run that final command under the machine's process manager: `launchd` on macOS, `systemd` on Linux, or the profile's existing service runner.

## Update protocol for existing consumers

When `main` changes, consumers need to update the local checkout **and restart the running sidecar**. A pulled repo does not update a long-running `artifactd` process until the service is restarted.

From the consumer machine/profile:

```bash
cd /path/to/artifactd
git fetch origin
git pull --ff-only origin main

. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m py_compile src/artifactd/*.py
pytest tests -q
git diff --check
```

Then restart the process that serves that profile:

- `launchd` on macOS: `sudo launchctl kickstart -k system/<artifactd-service-label>`
- `systemd` on Linux: `sudo systemctl restart <artifactd-service-name>`
- foreground/dev server: stop it and rerun the documented `artifactd --home ... serve ...` command

After restart, verify the served URL, not just the local checkout:

```bash
curl -o /dev/null -s -w '%{http_code}\n' https://<artifact-host>/
curl -o /dev/null -s -w '%{http_code}\n' https://<artifact-host>/<known-public-slug>
curl -o /dev/null -s -w '%{http_code}\n' https://<artifact-host>/<known-protected-slug>
```

Expected results for a password-protected workspace are: `/` returns `401` before login, public Things return `200`, protected Things return `401` before login. Browser-login to Workspace Home should show per-card **Share link** controls; authenticated HTML artifact pages should show the floating **Share link** toolbar; creating one should produce a full `https://.../<slug>?share=...` URL with a 7-day expiry. The toolbar must not appear for unauthenticated visitors or `?share=` recipients.

For agents/people consuming this repo, the handoff rule is: if a change matters to other consumers, commit it, push it to `origin/main`, and include these update/restart/verify steps in the handoff. Local-only fixes do not count as shipped.

## Deploy/register a Thing

For profile/workspace auth, use `workspaces register` rather than legacy public deploys:

```bash
artifactd workspaces register ./dist/my-dashboard \
  --profile safrin \
  --hermes-root "$HOME/.hermes" \
  --slug my-dashboard \
  --title "My Dashboard" \
  --description "Review surface generated by Safrin" \
  --tag dashboard \
  --tag review \
  --pinned \
  --capability artifact.describe \
  --capability artifact.archive
```

Multiple `--tag` values are normalized, deduped, shown as chips on Workspace Home, and filter with AND semantics when multiple tags are selected.

Useful metadata updates use the same underlying store. For a workspace profile, point `artifactd` at the workspace home:

```bash
artifactd --home "$WORKSPACE_HOME" describe my-dashboard --tag dashboard --tag safrin --tag review
artifactd --home "$WORKSPACE_HOME" describe my-dashboard --title "Updated title" --description "Updated searchable description"
```

You can also re-run `artifactd workspaces register ...` with the same slug to update the generated Thing from a new source directory.

## Tailscale deployment

For Safrin, prefer Tailscale over Cloudflare. There are two modes:

1. **Tailscale Serve** — private to the tailnet. This is the default recommendation.
2. **Tailscale Funnel** — public internet exposure through Tailscale. Use only when the artifact surface needs a public URL.

Start `artifactd` locally first, for example on `127.0.0.1:8789`, then expose it.

### Private tailnet URL

```bash
# Expose local artifactd to devices/users in the tailnet.
tailscale serve --bg 8789

# Inspect the assigned HTTPS URL and config.
tailscale serve status
```

Typical URL shape:

```text
https://<machine-name>.<tailnet-name>.ts.net/
```

Use that as the public base URL for Safrin's profile-local plugin if other tailnet devices should open generated Things:

```bash
artifactd workspaces install-plugin \
  --profile safrin \
  --runtime-path "$(command -v artifactd)" \
  --port 8789 \
  --public-base-url "https://<machine-name>.<tailnet-name>.ts.net" \
  --enable
```

### Public Tailscale Funnel URL

Only use Funnel if you intentionally want internet exposure:

```bash
tailscale funnel --bg 8789
tailscale funnel status
```

Security posture stays the same either way:

- Workspace Home and profile-auth Things require the workspace password/session.
- Share links are randomized and expire after 7 days by default.
- Do not create per-Thing password files for normal sharing.
- Do not expose a public deploy API.

### Reset Tailscale exposure

```bash
tailscale serve reset
tailscale funnel reset
```

## Cloudflare Tunnel

Cloudflare is still supported and is what Skylar's current Palmer/Echo public artifact surfaces use, but it is not required for Safrin.

Development quick tunnel:

```bash
cloudflared tunnel --url http://localhost:8787
```

Named tunnel with a stable hostname:

```bash
HOME=/Users/skylarpayne cloudflared tunnel create artifacts
HOME=/Users/skylarpayne cloudflared tunnel route dns --overwrite-dns <tunnel-id> artifacts.skylarbpayne.com
HOME=/Users/skylarpayne cloudflared tunnel --config ~/.cloudflared/artifacts.yml run <tunnel-id>
```

Use the tunnel ID explicitly when another `~/.cloudflared/config.yml` exists; relying on the tunnel name can accidentally target the default config's tunnel.

## Local CLI basics

Legacy/standalone artifact home mode still exists:

```bash
# Start local server
ARTIFACTD_COOKIE_SECRET="change-me" artifactd serve --port 8787

# Deploy public artifact
artifactd deploy ./demo.html --slug demo --title "Demo" --description "Visual review board" --tag demo

# Legacy standalone public artifact home; prefer workspace auth for normal agent Things
artifactd deploy ./dist --slug investor-memo --title "Investor Memo"

# Manage artifacts
artifactd list
artifactd list --status archived
artifactd describe demo --title "Demo v2" --description "Updated searchable description" --tag demo --tag review
artifactd archive demo
artifactd restore demo
artifactd prune --dry-run
artifactd delete demo
```

Default standalone storage:

```text
~/.hermes/artifacts/
├── artifacts.db
└── sites/<slug>/index.html
```

Override with:

```bash
ARTIFACTD_HOME=/path/to/artifacts artifactd list
artifactd --home /path/to/artifacts list
```

## Hermes Workspaces plugin

Workspaces are the profile-scoped path toward “one Home for things the agent made.” The integration is plugin-first and does not require Hermes core edits.

Useful commands:

```bash
artifactd workspaces install --profile palmer --password "<store outside git>"
artifactd workspaces status --profile palmer
artifactd workspaces home --profile palmer
artifactd workspaces smoke --profile palmer --password "<dev-only>"
artifactd workspaces import-legacy --profile palmer --from-home /Users/skylarpayne/.hermes/artifacts
artifactd workspaces register ./dist/daily.html --profile palmer --slug daily --title "Daily cockpit" --tag daily --tag planning
artifactd workspaces install-plugin --profile palmer --runtime-path /Users/skylarpayne/artifactd/.venv/bin/artifactd --port 8787 --enable
artifactd workspaces start --profile palmer --port 8787
```

Current Workspaces behavior:

- profile names are validated before paths are derived;
- generated Things default to profile/workspace auth;
- one workspace session unlocks profile-auth Things;
- `localStorage['artifactd.masterPassword']` is used only for browser convenience;
- share links are random, hash-stored server-side, and expire after 7 days by default;
- tags are flexible metadata and filters, not a rigid folder hierarchy;
- Home exposes Open, Share, Update, Pin, Requires action, Archive, tag chips, and tag facets;
- `GET /_workspace/home` returns dashboard JSON with profile bridge/capability metadata;
- `GET /_workspace/things` returns dashboard-friendly buckets/counts/tag facets;
- server-side capability actions require protected access, CSRF, explicit capability names, and audit rows.

## Action capability model

Artifacts can use normal browser interactivity freely: JavaScript, tabs, filters, calculators, annotations, localStorage checklists, and form UX.

Server-side effects go through the fixed capability layer:

```text
GET  /<slug>/_actions
POST /<slug>/_actions/<capability>
```

Current executable capabilities:

- `artifact.describe` — update title/description metadata.
- `artifact.archive` — archive the current artifact.
- `kanban.comment` — append to a Hermes Kanban task.
- `kanban.create_task` — create a Hermes Kanban task.

`draft.email` exists only as an approval-gated placeholder. Sends, publishing, scheduling, purchases, and credentials stay outside direct artifact execution.

## Current Skylar deployments

Palmer:

```text
storage: /Users/skylarpayne/.hermes/profiles/palmer/workspaces
legacy path: /Users/skylarpayne/.hermes/artifacts -> profile workspace symlink
origin:  http://127.0.0.1:8787
public:  https://artifacts.skylarbpayne.com/<slug>
service: /Library/LaunchDaemons/com.skylar.artifactd.plist
```

Echo:

```text
storage: /Users/skylarpayne/.hermes/profiles/echo/artifacts
origin:  http://127.0.0.1:8788
public:  https://artifacts.agoracomms.com/<slug>
service: /Library/LaunchDaemons/com.skylar.echo-artifactd.plist
```

Those use Cloudflare today. Safrin should use the Tailscale section above unless there is an explicit reason to make artifacts public.

## Security notes

- Deploy/register is local CLI/plugin only. There is no public deploy API.
- Workspace/master passwords are stored as hashes only.
- Browser password auto-fill uses `localStorage` for UX; the server never stores plaintext passwords.
- Default profile-auth Things should not get artifact-specific password files.
- Share tokens are random, stored only as hashes, and expire.
- Server-side actions require auth + CSRF + declared capability + audit logging.
- Slugs are sanitized and file serving is constrained to the artifact root.
- Artifact HTML may run browser-side JavaScript. Server-side execution is limited to the fixed capability registry.

## Test

```bash
. .venv/bin/activate
pytest tests -q
python -m py_compile src/artifactd/*.py
git diff --check
```
