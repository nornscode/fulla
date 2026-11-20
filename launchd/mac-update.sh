#!/bin/sh
# Deploy the Mac worker: fast-forward the checkout to origin/main and, if it
# moved, sync dependencies into Fulla's own venv (mac-python.sh) and restart
# the launchd job. Run by
# com.fulla.mac.update.plist every hour, or by hand after a merge.
# The checkout this runs in must stay on main with no local edits.
set -eu
PATH="$HOME/.local/bin:$PATH"   # where uv's installer puts it
cd "$(dirname "$0")/.."
before=$(git rev-parse HEAD)
git pull -q --ff-only
after=$(git rev-parse HEAD)
if [ "$before" = "$after" ]; then
  exit 0
fi
echo "$(date '+%F %T') updated $before -> $after"
sh launchd/mac-python.sh >/dev/null
launchctl kickstart -k "gui/$(id -u)/com.fulla.mac"
