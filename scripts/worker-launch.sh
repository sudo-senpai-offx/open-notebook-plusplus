#!/bin/sh
# Start the commands worker with a concurrency backstop.
#
# Why this is a file and not a command= line in supervisord.conf:
# supervisord parses its config with ExtendedInterpolation, which expands
# $VAR and ${VAR} in `command=` *before* a shell ever sees the string. A
# shell variable assigned by an earlier `;` in the same command is therefore
# not protected from it, and this once produced a concurrency record
# containing supervisord's entire environment mapping. A separate script is
# passed as an opaque path, so no quoting has to survive a parser that is not
# a shell.
#
# What the number means, and why it is a constant
# ----------------------------------------------
# It does NOT decide concurrency any more. The dispatcher admits jobs from
# `held` to `new` up to a live window, so the worker's own limit only ever
# binds if the dispatcher is broken - which is exactly what a backstop is
# for. Reading the throttle file here as well would be actively wrong: a
# window lowered to 1 at runtime must not shrink a semaphore that was fixed
# at process start, and a window raised to 8 must not be capped by a worker
# that happened to start at 3.
#
# Per-device ceilings (the phone's thermal limit) live in the *dispatcher*,
# as OPEN_NOTEBOOK_WINDOW_CAP, where they can be lowered while running.

set -eu

# Must be >= the highest window the dispatcher will ever admit, or the
# semaphore becomes the real limit and the gate stops governing anything.
BACKSTOP="${OPEN_NOTEBOOK_WORKER_BACKSTOP:-8}"
case "$BACKSTOP" in
    ''|*[!0-9]*) BACKSTOP=8 ;;
esac

exec uv run --no-sync surreal-commands-worker \
    --import-modules commands --max-tasks "$BACKSTOP"