#!/bin/sh
# Vehicle 2 chat setup — run INSIDE the proot guest, with the stack up.
#
#   sh /root/chat-setup.sh
#
# Idempotent. Registers llama-server (already running in Termux on :8080) as
# the app's OpenAI-compatible provider, then discovers its models.
#
# Why env vars and not the UI: api/routers/models.py:166 enables the
# `openai_compatible` provider purely on OPENAI_COMPATIBLE_BASE_URL +
# OPENAI_COMPATIBLE_API_KEY being present in os.environ. Both are read at
# boot, hence the restart.

set -u

APP=/root/app/open-notebook-plusplus
ENV_FILE="$APP/.env"
BASE_URL="http://127.0.0.1:8080/v1"
API_KEY="sk-local"
API="http://127.0.0.1:5055"

fail() { echo "[chat] FATAL: $*" >&2; exit 1; }

echo "[chat] 1/5 checking llama-server is reachable from the guest"
curl -sf -m 5 http://127.0.0.1:8080/health >/dev/null 2>&1 \
  || fail "llama-server not answering on 127.0.0.1:8080 — is it running? (Termux: ~/bin/onb)"
echo "      ok"

echo "[chat] 2/5 writing provider config to .env"
if grep -q '^OPENAI_COMPATIBLE_BASE_URL=' "$ENV_FILE" 2>/dev/null; then
  echo "      already present, leaving as-is"
else
  umask 077
  printf '%s\n' \
    "OPENAI_COMPATIBLE_BASE_URL=$BASE_URL" \
    "OPENAI_COMPATIBLE_API_KEY=$API_KEY" >> "$ENV_FILE"
  echo "      added"
fi
grep '^OPENAI_COMPATIBLE' "$ENV_FILE"

echo "[chat] 3/5 restarting api + worker (they read env at boot)"
sh /root/termux-stack.sh down >/dev/null 2>&1
sh /root/termux-stack.sh up >/dev/null 2>&1
sleep 3
curl -sf -m 10 "$API/health" >/dev/null 2>&1 || fail "API did not come back up"

echo "[chat] 4/5 provider availability"
curl -s -m 10 "$API/api/models/providers" \
  | tr ',' '\n' | grep -i 'openai_compatible' | head -2 \
  || echo "      WARNING: openai_compatible NOT listed — env var did not reach the process"

echo "[chat] 5/5 syncing models from llama-server"
curl -s -m 30 -X POST "$API/api/models/sync/openai_compatible"; echo

echo "[chat] models now registered:"
curl -s -m 10 "$API/api/models" | head -c 600; echo
echo "[chat] done — set default_chat_model next"