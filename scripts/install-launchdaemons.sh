#!/usr/bin/env bash
set -euo pipefail

repo="/Users/skylarpayne/artifactd"
user="skylarpayne"
group="staff"
daemon_dir="/Library/LaunchDaemons"
legacy_agent_dir="/Users/skylarpayne/Library/LaunchAgents"
logs="/Users/skylarpayne/Library/Logs/artifactd"
artifacts_home="/Users/skylarpayne/.hermes/artifacts"
tunnel_id="d8df3cd1-438e-47c9-996f-2c5d2b271bf1"
labels=(com.skylar.artifactd com.skylar.artifactd-tunnel)

if [[ "$(id -u)" != "0" ]]; then
  echo "Re-running with sudo because LaunchDaemons live in /Library/LaunchDaemons and load into the system domain..."
  exec sudo "$0" "$@"
fi

mkdir -p "$daemon_dir" "$logs" "$artifacts_home"
chown "$user:$group" "$logs" "$artifacts_home"

if [[ ! -f "$artifacts_home/.cookie-secret" ]]; then
  umask 077
  /usr/bin/python3 - <<'PY'
import pathlib, secrets
path = pathlib.Path('/Users/skylarpayne/.hermes/artifacts/.cookie-secret')
path.write_text(secrets.token_urlsafe(48), encoding='utf-8')
PY
  chown "$user:$group" "$artifacts_home/.cookie-secret"
  chmod 600 "$artifacts_home/.cookie-secret"
fi

for label in "${labels[@]}"; do
  install -o root -g wheel -m 0644 "$repo/launchd/$label.plist" "$daemon_dir/$label.plist"
  plutil -lint "$daemon_dir/$label.plist"
done

# Stop any stale GUI LaunchAgent copies from the earlier laptop-oriented setup, then remove them.
for label in "${labels[@]}"; do
  legacy_plist="$legacy_agent_dir/$label.plist"
  if [[ -f "$legacy_plist" ]]; then
    launchctl bootout "gui/$(id -u "$user")" "$legacy_plist" >/dev/null 2>&1 || true
    rm -f "$legacy_plist"
  fi
done

# Stop manually-started foreground/session copies before launchd owns the processes.
pkill -u "$user" -f '/Users/skylarpayne/artifactd/.venv/bin/artifactd .*serve --host 127\.0\.0\.1 --port 8787' >/dev/null 2>&1 || true
pkill -u "$user" -f '/opt/homebrew/bin/cloudflared .*--config /Users/skylarpayne/.cloudflared/artifacts\.yml run' >/dev/null 2>&1 || true

for label in "${labels[@]}"; do
  launchctl bootout "system/$label" >/dev/null 2>&1 || true
  launchctl bootstrap system "$daemon_dir/$label.plist"
  launchctl kickstart -k "system/$label"
done

for label in "${labels[@]}"; do
  launchctl print "system/$label" >/dev/null
done

for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8787/ >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8787/ >/dev/null

for _ in {1..30}; do
  if curl -fsS https://artifacts.skylarbpayne.com/smoke-live | grep -q 'artifactd is live'; then
    break
  fi
  sleep 1
done
curl -fsS https://artifacts.skylarbpayne.com/smoke-live | grep -q 'artifactd is live'
HOME=/Users/skylarpayne /opt/homebrew/bin/cloudflared tunnel info "$tunnel_id" >/dev/null

echo "artifactd LaunchDaemons loaded in the system domain"
echo "public smoke: https://artifacts.skylarbpayne.com/smoke-live"
