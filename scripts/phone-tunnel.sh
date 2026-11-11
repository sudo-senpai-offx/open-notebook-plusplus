#!/usr/bin/env bash
# (Re)establish the adb tunnels this repo's tooling depends on, then verify.
#
#   scripts/phone-tunnel.sh          # dev box -> phone loopback
#   scripts/phone-tunnel.sh --check  # verify only, don't re-add
#
# `adb forward` is per-connection: unplugging or reconnecting the phone
# CLEARS every forward silently. The symptom is a ladder run reporting
# "Connection refused" on all prompts, which looks like a model failure
# and is not. Run this before any phone-side measurement.
#
# Why forwards and not LAN: llama-server binds 127.0.0.1 inside Termux,
# so it is unreachable from the network even though the app's API on
# :5055 is. The tunnel is the only way to reach the model from here.

set -uo pipefail

PHONE_IP="${PHONE_IP:-22.204.59.238}"
LLAMA_LOCAL=18080
API_LOCAL=15055
# SurrealDB speaks HTTP as well as its own WS endpoint, which makes it
# queryable directly from here. `adb forward --remove-all` below used to
# wipe this port without restoring it, so any DB inspection that ran after
# re-tunnelling failed with an empty body and looked like a dead database.
DB_LOCAL=18000
# Embedding server, a second llama-server because --embeddings disables
# chat on the same process. Optional: absent, this reports DOWN without
# failing the check, since chat and RAG are independently useful.
EMBED_LOCAL=18081
# Frontend port for phone UI access over mobile data.
FRONTEND_LOCAL=18502
# SSH port for phone access over mobile data (Termux sshd on port 8022).
SSH_LOCAL=28022

check() {
  local ok=0
  # Assign then override: `$(... || echo 000)` yields curl's own 000 plus
  # the fallback, so a refused connection printed as 000000.
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${LLAMA_LOCAL}/health" 2>/dev/null) || code=000
  [ "$code" = "200" ] && echo "llama  127.0.0.1:${LLAMA_LOCAL} -> phone :8080   OK" || { echo "llama  tunnel DOWN (HTTP $code)"; ok=1; }
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_LOCAL}/health" 2>/dev/null) || code=000
  [ "$code" = "200" ] && echo "api    127.0.0.1:${API_LOCAL} -> phone :5055   OK" || { echo "api    tunnel DOWN (HTTP $code)"; ok=1; }
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${DB_LOCAL}/version" 2>/dev/null) || code=000
  [ "$code" = "200" ] && echo "db     127.0.0.1:${DB_LOCAL} -> phone :8000   OK" || { echo "db     tunnel DOWN (HTTP $code)"; ok=1; }
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${EMBED_LOCAL}/health" 2>/dev/null) || code=000
  [ "$code" = "200" ] && echo "embed  127.0.0.1:${EMBED_LOCAL} -> phone :8081   OK" || echo "embed  not running (optional)"
  code=$(ssh -o ConnectTimeout=6 -o BatchMode=yes -o StrictHostKeyChecking=accept-new -p "${SSH_LOCAL}" u0_a328@127.0.0.1 true 2>/dev/null) && echo "ssh    127.0.0.1:${SSH_LOCAL} -> phone :8022   OK" || { echo "ssh    tunnel DOWN (connection failed)"; ok=1; }
  code=$(curl -s -m 6 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${FRONTEND_LOCAL}/" 2>/dev/null) || code=000
  [ "$code" = "200" ] || [ "$code" = "307" ] && echo "ui     127.0.0.1:${FRONTEND_LOCAL} -> phone :8502   OK" || { echo "ui     tunnel DOWN (HTTP $code)"; ok=1; }
  return $ok
}

if [ "${1:-}" != "--check" ]; then
  if ! adb get-state >/dev/null 2>&1; then
    echo "no adb device - reconnect the phone and retry" >&2
    exit 2
  fi
  adb forward --remove-all >/dev/null 2>&1 || true
  adb forward "tcp:${LLAMA_LOCAL}" tcp:8080 >/dev/null
  adb forward "tcp:${API_LOCAL}"  tcp:5055 >/dev/null
  adb forward "tcp:${DB_LOCAL}"   tcp:8000 >/dev/null
  adb forward "tcp:${EMBED_LOCAL}" tcp:8081 >/dev/null
  adb forward "tcp:${FRONTEND_LOCAL}" tcp:8502 >/dev/null
  adb forward "tcp:${SSH_LOCAL}" tcp:8022 >/dev/null
  echo "tunnels re-established"
fi

check