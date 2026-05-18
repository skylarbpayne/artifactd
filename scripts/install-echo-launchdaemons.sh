#!/usr/bin/env bash
set -euo pipefail

repo="/Users/skylarpayne/artifactd"
user="skylarpayne"
group="staff"
daemon_dir="/Library/LaunchDaemons"
legacy_agent_dir="/Users/skylarpayne/Library/LaunchAgents"
logs="/Users/skylarpayne/Library/Logs/echo-artifactd"
artifacts_home="/Users/skylarpayne/.hermes/profiles/echo/artifacts"
cloudflare_config_src="$repo/cloudflare/echo-artifacts.yml"
cloudflare_config_dst="/Users/skylarpayne/.cloudflared/echo-artifacts.yml"
tunnel_id="f49229f5-9211-4551-b56b-d28d3c3ea99b"
labels=(com.skylar.echo-artifactd com.skylar.echo-artifactd-tunnel)

if [[ "$(id -u)" != "0" ]]; then
  echo "Re-running with sudo because LaunchDaemons live in /Library/LaunchDaemons and load into the system domain..."
  exec sudo "$0" "$@"
fi

mkdir -p "$daemon_dir" "$logs" "$artifacts_home" /Users/skylarpayne/.cloudflared
chown "$user:$group" "$logs" "$artifacts_home"

if [[ ! -f "$artifacts_home/.cookie-secret" ]]; then
  umask 077
  /usr/bin/python3 - <<'PY'
import pathlib, secrets
path = pathlib.Path('/Users/skylarpayne/.hermes/profiles/echo/artifacts/.cookie-secret')
path.write_text(secrets.token_urlsafe(48), encoding='utf-8')
PY
  chown "$user:$group" "$artifacts_home/.cookie-secret"
  chmod 600 "$artifacts_home/.cookie-secret"
fi

install -o "$user" -g "$group" -m 0600 "$cloudflare_config_src" "$cloudflare_config_dst"

for label in "${labels[@]}"; do
  install -o root -g wheel -m 0644 "$repo/launchd/$label.plist" "$daemon_dir/$label.plist"
  plutil -lint "$daemon_dir/$label.plist"
done

# Stop any stale GUI LaunchAgent copies if someone experimented with a user-session setup.
for label in "${labels[@]}"; do
  legacy_plist="$legacy_agent_dir/$label.plist"
  if [[ -f "$legacy_plist" ]]; then
    launchctl bootout "gui/$(id -u "$user")" "$legacy_plist" >/dev/null 2>&1 || true
    rm -f "$legacy_plist"
  fi
done

# Stop manually-started foreground/session copies before launchd owns the processes.
pkill -u "$user" -f '/Users/skylarpayne/artifactd/.venv/bin/artifactd .*serve --host 127\.0\.0\.1 --port 8788' >/dev/null 2>&1 || true
pkill -u "$user" -f '/opt/homebrew/bin/cloudflared .*--config /Users/skylarpayne/.cloudflared/echo-artifacts\.yml run' >/dev/null 2>&1 || true

for label in "${labels[@]}"; do
  launchctl bootout "system/$label" >/dev/null 2>&1 || true
  launchctl bootstrap system "$daemon_dir/$label.plist"
  launchctl kickstart -k "system/$label"
done

for label in "${labels[@]}"; do
  launchctl print "system/$label" >/dev/null
done

for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:8788/echo-live | grep -q 'Echo artifactd is live'; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8788/echo-live | grep -q 'Echo artifactd is live'

if curl -fsS https://artifacts.agoracomms.com/echo-live | grep -q 'Echo artifactd is live'; then
  echo "public smoke ok: https://artifacts.agoracomms.com/echo-live"
else
  echo "WARNING: public smoke not reachable yet. If DNS is not configured, create CNAME artifacts -> ${tunnel_id}.cfargotunnel.com in the agoracomms.com Cloudflare zone, then retry the public curl." >&2
fi
HOME=/Users/skylarpayne /opt/homebrew/bin/cloudflared tunnel info "$tunnel_id" >/dev/null

echo "Echo artifactd LaunchDaemons loaded in the system domain"
echo "local smoke: http://127.0.0.1:8788/echo-live"
echo "public target: https://artifacts.agoracomms.com/echo-live"
