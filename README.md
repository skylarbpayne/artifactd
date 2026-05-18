# artifactd

Tiny local artifact server for agent-produced HTML artifacts.

## What it does

- Deploy a single `.html` file or a directory with `index.html`.
- Serve artifacts locally from SQLite metadata + static files.
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
ARTIFACTD_PUBLIC_BASE_URL="https://artifacts.skylarbpayne.com" artifactd deploy ./demo.html --slug demo

# Deploy a protected artifact
ARTIFACTD_PUBLIC_BASE_URL="https://artifacts.skylarbpayne.com" artifactd deploy ./dist --slug investor-memo --password "secret"

# Manage artifacts
artifactd list
artifactd protect demo --password "new-secret"
artifactd unprotect demo
artifactd delete demo
```

When `ARTIFACTD_PUBLIC_BASE_URL` or `--public-base-url` is set, deploy/list also prints the public HTTPS URL.

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

The production setup on Skylar's Mac uses:

```text
/Users/skylarpayne/.hermes/artifacts/.cookie-secret
/Users/skylarpayne/.cloudflared/artifacts.yml
/Users/skylarpayne/Library/LaunchAgents/com.skylar.artifactd.plist
/Users/skylarpayne/Library/LaunchAgents/com.skylar.artifactd-tunnel.plist
```

Repo copies of the LaunchAgent plists live under `launchd/`; `scripts/install-launchagents.sh` installs and bootstraps them from a normal GUI login shell.

Then artifacts are available as:

```text
https://artifacts.skylarbpayne.com/<slug>
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
