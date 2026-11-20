#!/bin/sh
# Deploy the host worker: fast-forward the checkout to origin/main and, if it
# moved, sync dependencies and restart the systemd user service. Run by
# fulla-host-update.timer every hour, or by hand after a merge.
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
uv sync -q
systemctl --user restart fulla-host
