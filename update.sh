#!/usr/bin/env bash
# Solar Pi one-shot updater. Run any time on the Pi:
#
#     ~/solardash/update.sh            # pull latest from GitHub; restart only if something changed
#     ~/solardash/update.sh --force    # restart even when already up to date (e.g. after editing
#                                      # solardash.env or packs.conf, which git doesn't track)
#
# It pulls the latest commit, reinstalls Python deps only if requirements.txt changed, restarts the
# systemd service, and confirms it came back up. The whole script is wrapped in { } so bash reads it
# fully before executing — a git pull that rewrites this file mid-run can't corrupt the run.
{
set -u
SERVICE=solardash

FORCE=0
case "${1:-}" in
  -f|--force) FORCE=1 ;;
  "") ;;
  *) echo "unknown option: $1  (use --force)"; exit 2 ;;
esac

# systemctl --user needs this pointed at the user bus when run over a plain SSH session.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Operate on the repo this script lives in, so it works no matter where it's called from.
cd "$(dirname "$(readlink -f "$0")")" || { echo "cannot find repo dir"; exit 1; }

echo "== Solar Pi update =="
echo "repo: $(pwd)"
before=$(git rev-parse HEAD 2>/dev/null) || { echo "not a git repo"; exit 1; }

echo "-- fetching latest --"
if ! git pull --ff-only; then
  echo "git pull failed — local changes or diverged history. Resolve, then re-run."
  exit 1
fi
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ] && [ "$FORCE" -eq 0 ]; then
  echo "already up to date at $(git rev-parse --short HEAD) — nothing to deploy."
  echo "(pass --force to restart anyway)"
  exit 0
fi

if [ "$before" != "$after" ]; then
  echo "updated $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")"
  # Reinstall deps only when they actually changed, so the common case stays fast.
  if git diff --name-only "$before" "$after" | grep -q '^requirements.txt$'; then
    echo "-- requirements.txt changed: installing deps --"
    ./.venv/bin/pip install -q -r requirements.txt || echo "WARNING: pip install failed"
  fi
fi

echo "-- restarting $SERVICE --"
systemctl --user restart "$SERVICE"
sleep 2
state=$(systemctl --user is-active "$SERVICE" || true)
echo "service: $state"
if [ "$state" != "active" ]; then
  echo "-- service is not active; last log lines --"
  journalctl --user -u "$SERVICE" -n 15 --no-pager || true
  exit 1
fi
echo "now on: $(git log -1 --format='%h  %s')"
echo "done."
}
