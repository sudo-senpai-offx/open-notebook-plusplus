#!/bin/sh
# Resolve worker concurrency and start the commands worker.
#
# Why this is a file and not a command= line in supervisord.conf:
# supervisord parses its config with ExtendedInterpolation, which
# expands $VAR and ${VAR} in `command=` *before* a shell ever sees the
# string. A shell variable set by an earlier `;` in the same command is
# therefore not protected from it - the one time this ran inline, the
# value in the printf was replaced by supervisord's whole environment
# mapping and the worker recorded a garbage concurrency that the API
# (rightly) refused to parse. A separate script is passed as an opaque
# path, so no amount of quoting has to survive a parser that is not a
# shell.
#
# Two sources, in order of authority:
#   1. throttle.env  - the persisted preference written by
#      POST /processing/throttle. Read with grep, never sourced: a
#      corrupt file is then ignored, not executed.
#   2. the environment - a static default from compose/the launcher.
#
# Whatever wins is recorded in throttle.running. That record is the
# worker's own testimony about what it started with, and it is what the
# API reports: reading the environment here would describe the
# pre-override number, because overriding it is this script's job.

set -eu

data_dir="${OPEN_NOTEBOOK_THROTTLE_DIR:-/app/data}"
preference="$data_dir/throttle.env"
running="$data_dir/throttle.running"

tasks=$(grep -s '^OPEN_NOTEBOOK_WORKER_MAX_TASKS=[1-8]$' "$preference" 2>/dev/null | tail -1 | cut -d= -f2) || tasks=""

# A platform cap may lower this; it is a hardware limit, not a
# preference, so it is applied after the preference is read.
cap="${OPEN_NOTEBOOK_WORKER_MAX_TASKS_CAP:-0}"

if [ -z "$tasks" ]; then
    tasks="${OPEN_NOTEBOOK_WORKER_MAX_TASKS:-1}"
fi
case "$tasks" in
    ''|*[!0-9]*) tasks=1 ;;
esac
[ "$tasks" -ge 1 ] 2>/dev/null || tasks=1

if [ "$cap" -ge 1 ] 2>/dev/null && [ "$tasks" -gt "$cap" ]; then
    tasks=$cap
fi

mkdir -p "$data_dir" 2>/dev/null || true
printf 'OPEN_NOTEBOOK_WORKER_MAX_TASKS=%s\n' "$tasks" >"$running" 2>/dev/null || true

exec uv run --no-sync surreal-commands-worker \
    --import-modules commands --max-tasks "$tasks"