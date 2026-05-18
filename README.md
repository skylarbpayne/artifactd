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
ARTIFACTD_COOKIE_SECRET="change-me" artifactd serve --port 8787

# Deploy a public artifact
artifactd deploy ./demo.html --slug demo

# Deploy a protected artifact
artifactd deploy ./dist --slug investor-memo --password "secret"

# Manage artifacts
artifactd list
artifactd protect demo --password "new-secret"
artifactd unprotect demo
artifactd delete demo
```

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
cloudflared tunnel create artifacts
cloudflared tunnel route dns artifacts artifacts.skylarpayne.com
cloudflared tunnel run artifacts
```

There is a starter config at `cloudflare/artifacts.example.yml` for a durable named tunnel.

Then artifacts are available as:

```text
https://artifacts.skylarpayne.com/<slug>
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
