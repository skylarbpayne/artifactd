#!/usr/bin/env bash
set -euo pipefail

echo "install-launchagents.sh is deprecated for this headless Mac mini. Using LaunchDaemons instead."
exec /Users/skylarpayne/artifactd/scripts/install-launchdaemons.sh "$@"
