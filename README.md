# artifactd

Tiny local artifact server for agent-produced HTML artifacts.

## What it does

- Deploy a single `.html` file or a directory with `index.html`.
- Serve artifacts locally from SQLite metadata + static files.
- Browse artifacts from a searchable home page.
- Store searchable titles and descriptions for each artifact.
- Protect each artifact with its own password.
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

# Manage artifacts
artifactd list
artifactd describe demo --title "Demo v2" --description "Updated searchable description"
artifactd protect demo --password "new-secret"
artifactd unprotect demo
artifactd delete demo
```

When `ARTIFACTD_PUBLIC_BASE_URL` or `--public-base-url` is set, deploy/list also prints the public HTTPS URL. The root path (`/`) is a searchable artifact home page; search matches slug, title, and description.

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
- Slugs are sanitized and file serving is constrained to the artifact root.
- Static files only. No server-side execution inside artifacts.

## Test

```bash
. .venv/bin/activate
pytest tests -q
```
