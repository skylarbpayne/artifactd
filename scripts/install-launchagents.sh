#!/usr/bin/env bash
set -euo pipefail

uid="$(id -u)"
repo="/Users/skylarpayne/artifactd"
launch_agents="/Users/skylarpayne/Library/LaunchAgents"
logs="/Users/skylarpayne/Library/Logs/artifactd"

mkdir -p "$launch_agents" "$logs" /Users/skylarpayne/.hermes/artifacts

if [[ ! -f /Users/skylarpayne/.hermes/artifacts/.cookie-secret ]]; then
  umask 077
  python3 - <<'PY'
import pathlib, secrets
path = pathlib.Path('/Users/skylarpayne/.hermes/artifacts/.cookie-secret')
path.write_text(secrets.token_urlsafe(48), encoding='utf-8')
PY
fi

cp "$repo/launchd/com.skylar.artifactd.plist" "$launch_agents/com.skylar.artifactd.plist"
cp "$repo/launchd/com.skylar.artifactd-tunnel.plist" "$launch_agents/com.skylar.artifactd-tunnel.plist"

plutil -lint "$launch_agents/com.skylar.artifactd.plist"
plutil -lint "$launch_agents/com.skylar.artifactd-tunnel.plist"

for label in com.skylar.artifactd com.skylar.artifactd-tunnel; do
  plist="$launch_agents/$label.plist"
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$plist"
  launchctl kickstart -k "gui/$uid/$label"
done

curl -fsS http://127.0.0.1:8787/ >/dev/null
HOME=/Users/skylarpayne cloudflared tunnel info d8df3cd1-438e-47c9-996f-2c5d2b271bf1 >/dev/null

echo "artifactd LaunchAgents loaded"
