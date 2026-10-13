#!/bin/sh
# Vehicle 2 chat setup — run INSIDE the proot guest.
#
#   sh /root/chat-setup.sh
#
# Idempotent, and deliberately does NOT restart anything.
#
# Why no restart: proot runs with --kill-on-exit, so every process started
# inside a login session dies when that session exits. An earlier version
# of this script called `termux-stack.sh down && up` — run from a second
# `proot-distro login`, it killed the stack belonging to the first session
# and started replacements owned by the second. Exiting that second session
# then reaped the replacements, leaving nothing running. Restarting is the
# operator's job: Ctrl+C the session running ~/bin/onb, then run it again.
# The env vars are read at boot, so a fresh start picks them up.
#
# Why env vars and not the UI: api/routers/models.py:166 enables the
# `openai_compatible` provider purely on OPENAI_COMPATIBLE_BASE_URL +
# OPENAI_COMPATIBLE_API_KEY being present in os.environ.

set -u

APP=/root/app/open-notebook-plusplus
ENV_FILE="$APP/.env"
BASE_URL="http://127.0.0.1:8080/v1"
API_KEY="sk-local"
API="http://127.0.0.1:5055"

echo "[chat] 1/4 llama-server reachable from the guest"
if curl -sf -m 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then
  echo "      ok"
else
  echo "      WARNING: llama-server not answering on 127.0.0.1:8080"
  echo "               (it runs in Termux, not the guest — is ~/bin/onb running?)"
fi

echo "[chat] 2/4 provider config in .env"
if grep -q '^OPENAI_COMPATIBLE_BASE_URL=' "$ENV_FILE" 2>/dev/null; then
  echo "      already present:"
  grep '^OPENAI_COMPATIBLE' "$ENV_FILE" | sed 's/^/        /'
  echo "      -> if the API was started BEFORE these lines existed, restart it"
else
  umask 077
  printf '%s\n' \
    "OPENAI_COMPATIBLE_BASE_URL=$BASE_URL" \
    "OPENAI_COMPATIBLE_API_KEY=$API_KEY" >> "$ENV_FILE"
  echo "      added (restart required):"
  grep '^OPENAI_COMPATIBLE' "$ENV_FILE" | sed 's/^/        /'
fi

if ! curl -sf -m 8 "$API/health" >/dev/null 2>&1; then
  echo "[chat] API not up at $API — start the stack, then re-run this script"
  exit 1
fi

echo "[chat] 3/4 provider availability in the running API"
if curl -s -m 10 "$API/api/models/providers" | tr ',' '\n' | grep -qi openai_compatible; then
  echo "      openai_compatible IS available"
else
  echo "      openai_compatible NOT available -> the API predates the .env change"
  echo "      Ctrl+C your ~/bin/onb session and run ~/bin/onb again"
  exit 1
fi

echo "[chat] 4/4 syncing models from llama-server"
curl -s -m 60 -X POST "$API/api/models/sync/openai_compatible"; echo
echo "[chat] done"