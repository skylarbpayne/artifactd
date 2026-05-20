# artifactd

Tiny local artifact server for agent-produced HTML artifacts.

## What it does

- Deploy a single `.html` file or a directory with `index.html`.
- Serve artifacts locally from SQLite metadata + static files.
- Browse active artifacts from a searchable home page, with archived artifacts separated at `/archive`.
- Store searchable titles, descriptions, lifecycle status, pinning, expiration metadata, and explicit action capabilities for each artifact.
- Protect artifacts with either legacy per-artifact passwords or the newer profile/workspace password session.
- Register generated workspace “Things” for Hermes Home-style open/share/update/pin/archive flows.
- Allow browser-side interactivity in artifacts: JS, filters, localStorage, forms, and visual review controls.
- Expose a small safe server-action layer for protected artifacts only (`/{slug}/_actions`) with CSRF, explicit capabilities, validation, and audit logs.
- Expose the local server through Cloudflare Tunnel.

## Install for development

```bash
cd /Users/skylarpayne/artifactd
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

## Use

```bash
# Start local server
ARTIFACTD_COOKIE_SECRET="change-me" \
ARTIFACTD_PUBLIC_BASE_URL="https://artifacts.skylarbpayne.com" \
artifactd serve --port 8787

# Deploy a public artifact
ARTIFACTD_PUBLIC_BASE_URL="https://artifacts.skylarbpayne.com" artifactd deploy ./demo.html --slug demo --title "Demo" --description "Visual review board for the demo"

# Deploy a protected artifact
ARTIFACTD_PUBLIC_BASE_URL="https://artifacts.skylarbpayne.com" artifactd deploy ./dist --slug investor-memo --title "Investor Memo" --description "Protected investor memo review surface" --password "secret"

# Deploy a protected action-capable artifact
ARTIFACTD_PUBLIC_BASE_URL="https://artifacts.skylarbpayne.com" artifactd deploy ./dashboard.html --slug project-dashboard --title "Project Dashboard" --description "Protected project command surface" --password "secret" --capability artifact.describe --capability artifact.archive --capability kanban.comment

# Manage artifacts
artifactd list
artifactd list --status archived
artifactd describe demo --title "Demo v2" --description "Updated searchable description"
artifactd describe demo --pinned --expires-at 1798761600
artifactd archive demo
artifactd restore demo
artifactd prune --dry-run
artifactd prune --apply
artifactd protect demo --password "new-secret"
artifactd unprotect demo
artifactd delete demo
```

When `ARTIFACTD_PUBLIC_BASE_URL` or `--public-base-url` is set, deploy/list also prints the public HTTPS URL. The root path (`/`) is a searchable active-artifact home page; `/archive` lists archived artifacts. Search matches slug, title, and description.

`prune --dry-run` previews lifecycle actions. `prune --apply` archives expired active artifacts first, skips pinned artifacts, and will not delete protected artifacts automatically; expired public artifacts are only deletable after they are already archived.

Default storage lives at:

```text
~/.hermes/artifacts/
├── artifacts.db
└── sites/<slug>/index.html
```

Override with:

```bash
ARTIFACTD_HOME=/path/to/artifacts artifactd list
# or
artifactd --home /path/to/artifacts list
```

## Hermes Workspaces plugin

Workspaces are the profile-scoped path toward “one Hermes Home for things the agent made.” The generated Things are HTML/JS/CSS surfaces; the installable integration is this `artifactd` sidecar/plugin package. It is cloneable, smoke-testable, and installable into a Hermes profile without patching Hermes core.

```bash
# From a clone or this checkout
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'

# Create/check a profile-scoped workspace home without touching production state
TMPDIR=$(mktemp -d)
artifactd workspaces smoke --profile echo --hermes-root "$TMPDIR/.hermes" --password "dev-only"
artifactd workspaces status --profile echo --hermes-root "$TMPDIR/.hermes"
artifactd workspaces start --profile echo --hermes-root "$TMPDIR/.hermes" --port 8788

# Install the profile-local Hermes plugin wrapper from this package
artifactd workspaces install-plugin \
  --profile echo \
  --hermes-root "$TMPDIR/.hermes" \
  --runtime-path "$(command -v artifactd)" \
  --port 8788 \
  --enable
```

The default profile layout is:

```text
~/.hermes/profiles/<profile>/workspaces/
├── artifacts.db
├── sites/<thing-slug>/index.html
└── .smoke-source/            # only created by smoke tests
```

Useful commands:

```bash
artifactd workspaces install --profile palmer --password "<store outside git>"
artifactd workspaces status --profile palmer
artifactd workspaces register ./dist/daily.html --profile palmer --slug daily --title "Daily"
artifactd workspaces install-plugin \
  --profile palmer \
  --runtime-path /Users/skylarpayne/artifactd/.venv/bin/artifactd \
  --port 8787 \
  --public-base-url https://artifacts.skylarbpayne.com \
  --enable
artifactd workspaces start --profile palmer --port 8787
artifactd --home ~/.hermes/profiles/palmer/workspaces serve --profile palmer --port 8787
```

Current Workspaces behavior:

- profile names are validated before paths are derived;
- `--hermes-root <root>` maps to `<root>/profiles/<profile>`;
- generated Things default to profile/workspace auth;
- `artifactd workspaces register` turns existing HTML files/directories into profile-auth Things;
- `artifactd workspaces install-plugin` writes a profile-local Hermes plugin wrapper under `$HERMES_HOME/plugins/artifactd_workspaces` and can enable it via `plugins.enabled`;
- the package also exposes a `hermes_agent.plugins` entry point named `artifactd_workspaces` for pip-installed Hermes plugin discovery;
- one workspace session can unlock profile-auth Things;
- single-Thing share override tokens are available for explicit sharing;
- legacy custom-password artifacts remain compatible;
- Home exposes Open, Share, Update, Pin, Requires action, and Archive controls;
- `GET /_workspace/things` returns dashboard-friendly buckets/counts.

This is the correct plugin-first path: `artifactd` is the separately installable sidecar/plugin package, generated Things are not Hermes plugins, and Hermes core is not modified for Workspaces.

## Cloudflare Tunnel

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

Use the tunnel ID explicitly when another `~/.cloudflared/config.yml` exists; relying on the tunnel name can accidentally target the default config's tunnel. Ask me how I know.

There is a starter config at `cloudflare/artifacts.example.yml` for a durable named tunnel.
The production setup on Skylar's headless Mac mini uses **system LaunchDaemons**, not GUI LaunchAgents:

```text
/Users/skylarpayne/.hermes/artifacts/.cookie-secret
/Users/skylarpayne/.cloudflared/artifacts.yml
/Library/LaunchDaemons/com.skylar.artifactd.plist
/Library/LaunchDaemons/com.skylar.artifactd-tunnel.plist
```

Install/reload both LaunchDaemons with:

```bash
scripts/install-launchdaemons.sh
```

The installer uses `sudo`, installs root-owned plists into `/Library/LaunchDaemons`, loads them into the `system` launchd domain, runs both daemons as `skylarpayne`, removes stale GUI LaunchAgent copies from the earlier laptop-oriented setup, and verifies local + tunnel health.

Repo copies of the LaunchDaemon plists live under `launchd/`; `scripts/install-launchagents.sh` is kept only as a deprecated compatibility wrapper that delegates to `scripts/install-launchdaemons.sh`.

Then artifacts are available as:

```text
https://artifacts.skylarbpayne.com/<slug>
```

## Interactivity model

Artifacts can use normal browser interactivity freely: JavaScript, tabs, filters, calculators, annotations, localStorage checklists, and form UX. That is still static artifact code running in the browser.

For server-side effects, use the explicit capability layer:

```bash
artifactd deploy ./dashboard.html \
  --slug project-dashboard \
  --password "secret" \
  --capability artifact.describe \
  --capability artifact.archive \
  --capability kanban.comment \
  --capability kanban.create_task
```

The protected artifact can then fetch:

```text
GET  /<slug>/_actions
POST /<slug>/_actions/<capability>
```

Current executable capabilities:

- `artifact.describe` — update title/description metadata.
- `artifact.archive` — archive the current artifact.
- `kanban.comment` — append to a specific Hermes Kanban task.
- `kanban.create_task` — create a Hermes Kanban task with explicit assignee/parents/priority.

`draft.email` exists only as an approval-gated placeholder and returns `approval required`. Sends, publishing, scheduling, purchases, and credentials stay outside direct artifact execution.

## Project dashboard generator

The repo includes a static cockpit generator for recurring project surfaces. It reads Skyvault project notes, Hermes Kanban tasks, artifact metadata, and CRM/entity wikilinks, then writes a deployable multi-page directory. Skyvault/Kanban stay truth; the generated pages are the visual/action surface.

```bash
cd /Users/skylarpayne/artifactd
. .venv/bin/activate
python scripts/project_dashboard_generator.py \
  --out dist/project-dashboards \
  --deploy \
  --password "$(cat /path/to/dashboard-password)"
```

Default output/deploy target:

```text
dist/project-dashboards/index.html
https://artifacts.skylarbpayne.com/project-dashboards
```

Initial dashboards are Hack the Valley and Our Wedding. The deployed artifact is protected and pinned by default when generated with `--deploy`.

## Echo / Agora Comms artifact surface

Echo uses the same `artifactd` codebase with isolated storage and a separate Cloudflare Tunnel:

```text
storage:  /Users/skylarpayne/.hermes/profiles/echo/artifacts
origin:   http://127.0.0.1:8788
public:   https://artifacts.agoracomms.com/<slug>
tunnel:   f49229f5-9211-4551-b56b-d28d3c3ea99b
config:   /Users/skylarpayne/.cloudflared/echo-artifacts.yml
plists:   /Library/LaunchDaemons/com.skylar.echo-artifactd.plist
          /Library/LaunchDaemons/com.skylar.echo-artifactd-tunnel.plist
```

Local deploy example:

```bash
/Users/skylarpayne/artifactd/.venv/bin/artifactd \
  --home /Users/skylarpayne/.hermes/profiles/echo/artifacts \
  --public-base-url https://artifacts.agoracomms.com \
  deploy ./demo.html --slug demo --port 8788
```

Install/reload Echo's system LaunchDaemons with sudo/root:

```bash
cd /Users/skylarpayne/artifactd
sudo scripts/install-echo-launchdaemons.sh
```

## Security notes

- Deploy is local CLI only in this MVP. There is no public deploy API.
- Passwords are stored as PBKDF2-SHA256 hashes with random salts.
- Artifact auth uses signed HttpOnly cookies scoped to the artifact path.
- Server-side action capabilities are disabled unless explicitly enabled on a protected artifact; every action requires artifact auth + CSRF and writes an audit row with a payload hash, not raw submitted content.
- Slugs are sanitized and file serving is constrained to the artifact root.
- Artifact HTML may run browser-side JavaScript. Server-side execution is limited to the fixed capability registry; there is no arbitrary generated-code execution and no public deploy API.

## Test

```bash
. .venv/bin/activate
pytest tests -q
```
