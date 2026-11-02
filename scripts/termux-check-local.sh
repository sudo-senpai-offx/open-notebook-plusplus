#!/data/data/com.termux/files/usr/bin/sh
# Check allow_local_processing flag.
#
# Precedence:
#   1. $HOME/.onb_allow_local file — written by the frontend PUT /settings
#      endpoint, accessible from outside proot. Works on cold boot when
#      SurrealDB isn't running yet.
#   2. SurrealDB record open_notebook:processing_flags — via proot, works
#      on warm/reboot cycles when SurrealDB is already running.
#   3. Default: 0 (disabled) — safe default for cold boot when nothing is
#      configured yet.
if [ -f "$HOME/.onb_allow_local" ]; then
  cat "$HOME/.onb_allow_local"
  exit 0
fi

if command -v proot-distro >/dev/null 2>&1; then
  result=$(proot-distro login ubuntu -- sh -c '
    python3 -c "
import urllib.request, urllib.parse, json
try:
    q = urllib.parse.quote("SELECT * FROM ONLY open_notebook:processing_flags")
    r = urllib.request.urlopen("http://127.0.0.1:8000/sql?query=" + q, timeout=3).read().decode()
    rows = json.loads(r)
    if rows and isinstance(rows, list) and len(rows) > 0:
        print(1 if rows[0].get("allow_local_processing", False) else 0)
    else:
        print(0)
except Exception:
    print(0)
" 2>/dev/null
  ' 2>/dev/null) || echo 0
  echo "$result"
  exit 0
fi

echo 0
