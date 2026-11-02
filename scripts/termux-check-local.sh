#!/data/data/com.termux/files/usr/bin/sh
# Check allow_local_processing flag from SurrealDB inside proot.
# Prints "1" if enabled, "0" if disabled. Defaults to "1" on any error
# (e.g. cold boot when SurrealDB isn't running yet).
set -u

if ! command -v proot-distro >/dev/null 2>&1; then
  echo 1
  exit 0
fi

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
' 2>/dev/null) || echo 1

echo "$result"
