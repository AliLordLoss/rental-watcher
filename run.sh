#!/usr/bin/env bash
# Runs all rental watchers once. Intended for cron, e.g.:
#   0 * * * *  cd /path/to/repo && ./run.sh >> watcher.log 2>&1
set -u
repo_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$repo_dir"

python_bin="$repo_dir/.venv/bin/python"
if [ ! -x "$python_bin" ]; then
    python_bin="python3"
fi

status=0
for watcher in pararius/pararius_watcher.py funda/funda_watcher.py; do
    echo "=== $(date '+%F %T') running $watcher ==="
    watcher_status=0
    if [ "$watcher" = "funda/funda_watcher.py" ] && [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null 2>&1; then
        if xvfb-run -a "$python_bin" "$watcher"; then
            watcher_status=0
        else
            watcher_status=$?
        fi
    elif "$python_bin" "$watcher"; then
        watcher_status=0
    else
        watcher_status=$?
    fi

    if [ "$watcher_status" -ne 0 ]; then
        echo "!!! $watcher failed with exit code $watcher_status"
        status=1
    fi
done
exit $status
