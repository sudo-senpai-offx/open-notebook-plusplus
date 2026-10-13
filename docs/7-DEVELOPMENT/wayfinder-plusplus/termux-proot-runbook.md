# SD680 proot step runbook (vehicle 2 — needs hands on-device)

Goal: FastAPI + SurrealDB running inside Ubuntu proot, Termux-native
llama.cpp + PWA shell around it. Bench numbers + thermal verdict already
clear this device; this step proves the server half on-device.

Two Termux sessions are used throughout: **session A** inside proot (where
services run), **session B** plain Termux (for `curl`, `free`, filesystem
inspection).

## 0. Prereqs (done previously)

llama.cpp built at `~/llama.cpp/build/bin/`, GGUF at `~/gguf/`. Keep them.

## 1. Install the Ubuntu userland

```sh
pkg install -y proot-distro
proot-distro install ubuntu
```

## 2. Inside Ubuntu: Python + SurrealDB (glibc userland — never bare Termux)

```sh
proot-distro login ubuntu
apt-get update && apt-get install -y python3 python3-pip curl
curl -SfL https://install.surrealdb.com | sh
surreal version
```

The binary is **`surreal`**, not `surrealdb`. The installer currently serves
v3.x, while `docker-compose.yml` pins `surrealdb/surrealdb:v2` — treat the
version gap as a finding, not a blocker (step 5 tests the wire).

> **Install ordering matters.** `apt-get install` is all-or-nothing: one
> un-downloadable `.deb` and it aborts *before* the unpack phase, so **zero**
> packages land. A 118 MB "mostly successful" log can leave the guest with no
> Python at all — and the guest needs Python for the API, not just the probe.
> `--no-install-recommends` cuts the request set from 118 MB to 13.7 MB.
>
> **Known-good outcome:** `ca-certificates` 403s because the pool path
> contains `~` and this network blocks it in plain HTTP. Fix in step 3a,
> before anything else.

## 3. Boot SurrealDB alone first (isolate variables)

### 3a. Repair the CA store (required before the server will start)

Symptom, verbatim:

```
ERROR surrealdb_server::cli: Could not create http client
Caused by:
    0: builder error
    1: unexpected error: No CA certificates were loaded from the system
```

SurrealDB's HTTP client refuses to construct without a TLS trust store.
Two equivalent fixes — prefer the first, it needs no network:

**Option 1 — copy Termux's bundle (works offline).** proot-distro exposes
the *whole Termux prefix* inside the guest, so Termux's Mozilla bundle is
reachable from session A:

```sh
mkdir -p /etc/ssl/certs
cp /data/data/com.termux/files/usr/etc/tls/cert.pem /etc/ssl/certs/ca-certificates.crt
chmod 644 /etc/ssl/certs/ca-certificates.crt
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
wc -c /etc/ssl/certs/ca-certificates.crt   # expect 189462
```

> **Do not** try to fix this by copying from session B into
> `$PREFIX/var/lib/proot-distro/ubuntu/rootfs/...`. That layout guess is
> wrong (no such tree), `mkdir -p` will happily create a phantom
> directory, and the guest will not see it. Worse, `rm -rf` on that path
> is destructive until the real rootfs location is confirmed. Copy from
> inside proot instead.

**Option 2 — get the real package (preferred).** The network blocks any
plain-HTTP request whose path contains `~`, in escaped *or* raw form.
Verified on the LAN, same URL, five variants:

| Variant | Result |
|---|---|
| `http://` + `~` | 403 |
| `http://` + `%7e` | 403 |
| `http://` + `%7E` | 403 |
| `https://` + `~` | **200** (138 514 bytes) |
| `http://` control, no `~` (openssl) | 200 |

So switching apt to HTTPS is the real fix, and it also pre-empts every
future `~`-versioned package. Do this before any install:

```sh
sed -i 's|http://ports.ubuntu.com|https://ports.ubuntu.com|g' \
  /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list 2>/dev/null
grep -r 'URIs' /etc/apt/sources.list.d/ | head -3
apt-get update
```

One-off alternative if the sources rewrite is unwelcome — pre-seed the
cache so apt never makes the request (verified size 138 514):

```sh
cd /var/cache/apt/archives
curl -SfLO 'https://ports.ubuntu.com/ubuntu-ports/pool/main/c/ca-certificates/ca-certificates_20260601~26.04.1_all.deb'
ls -l ca-certificates*
apt-get install -y --no-install-recommends python3 python3-pip
```

Once the package is really installed, drop Option 1's workaround — and
**remove the copied file**, or `update-ca-certificates` will merge
Termux's whole bundle on top of Ubuntu's (a `rehash: warning: skipping
ca-certificates.crt, it does not contain exactly one certificate`
line means exactly that):

```sh
rm -f /usr/local/share/ca-certificates/termux.crt
update-ca-certificates
unset SSL_CERT_FILE
ls -l /etc/ssl/certs/ca-certificates.crt   # expect ~200 KB
```

### 3b. Start with the low-memory envelope

Do **not** start with defaults. v3 sizes RocksDB's cache at ~92% of RAM —
measured on-device at `total memory limit: 3188940800` (3.0 GB) and
`block cache size: 2920505344B`, which would fight llama.cpp for memory.

```sh
SURREAL_ROCKSDB_BLOCK_CACHE_SIZE=33554432 \
SURREAL_HNSW_CACHE_SIZE=33554432 \
SURREAL_ROCKSDB_WRITE_BUFFER_SIZE=16777216 \
SURREAL_ROCKSDB_MAX_WRITE_BUFFER_NUMBER=2 \
SURREAL_ROCKSDB_JOBS_COUNT=2 \
SURREAL_ROCKSDB_THREAD_COUNT=1 \
surreal start --user root --pass root rocksdb:~/snb-data/test.db
```

Same raw byte counts as the `docker-compose.yml` B4 envelope (v2 silently
ignores MiB suffixes). Expect in the log: `Background thread count: 1`,
`Maximum background jobs count: 2`, `total memory limit: 67108864`,
`block cache size: 33554432B`.

### 3c. Verify from session B

```sh
curl -s -i http://127.0.0.1:8000/health | head -3
curl -s http://127.0.0.1:8000/version; echo
free -m
```

`curl -s http://.../health` printing **nothing is correct**, not a failure:
v3 returns `HTTP 200` with `content-length: 0` (docs example uses
`curl -I`). Use `-i`/`-I` to see the status line.

## 4. Confirmed on-device results (SD680, 2026-10-13)

| Item | Result |
|---|---|
| `surreal version` | `3.3.3 for linux on aarch64` |
| RocksDB store | `Started rocksdb kvs store`, data at `/root/snb-data` |
| Envelope honoured | 64 MB total limit, 32 MB block cache, 1 thread, 2 jobs |
| RAM cost | `used` 4344 → 4435 MB, `available` 2905 → 2822 MB (~90 MB) |
| `/rpc` route | unchanged in v3 (WebSocket RPC still served at `/rpc`) |
| CA store | apt over **HTTPS** (see 3a); Python 3.14.4 + client 2.0.0 installed |
| **SDK 2.0.0 → server 3.3.3** | **`PROBE_OK`** — auth, `use`, `CREATE`, `SELECT` all round-trip |

## 5. Client/server compatibility probe — RESULT: compatible

Outcome (SD680, 2026-10-13): client `surrealdb==2.0.0` (pinned in
`uv.lock`) speaks server `3.3.3` correctly over WebSocket `/rpc`.
Sign-in, namespace/database selection, `CREATE` and `SELECT` all
round-trip. **No server pin is required for vehicle 2** — the official
installer ships v3 and it works with our client as-is.

`docker-compose.yml` keeps `surrealdb/surrealdb:v2` for the Docker path
regardless: bumping the primary deployment's database major is a
data-directory migration decision (existing `surreal_data/` RocksDB
files), not a compatibility question. Verified-compatible ≠
safe-to-upgrade-in-place. Revisit when someone is willing to test the
upgrade against a copy of real data.

The commands below are kept for reproducibility.

Client is pinned `surrealdb==2.0.0` (`uv.lock`). The v3 server is a newer
major; the RPC path is unchanged but the protocol has evolved. One
round-trip settles it.

**Use the guest interpreter by absolute path.** Inside proot, Termux's
`bin` precedes `/usr/bin`, so bare `pip`/`python` is Termux's — its build
is tagged `aarch64-linux-android`, which matches neither `manylinux` nor
`musllinux`, so pip silently rejects every wheel and falls back to the
sdist. That sdist needs `maturin` + Rust, and
`aarch64-unknown-linux-android` is not a rustup target. **Result: the
`pip install` fails looking like an ARM problem when it is a PATH
problem.** Anything that must live in the guest gets `/usr/bin/...`.

```sh
/usr/bin/python3 -V                                    # 3.14.4
/usr/bin/python3 -m pip install --only-binary :all: --break-system-packages 'surrealdb==2.0.0'
```

`--only-binary :all:` makes a missing wheel fail loudly instead of
quietly downloading an sdist and spending minutes discovering it needs
Rust. `--break-system-packages` is required: Ubuntu marks the system
Python externally-managed and `python3-venv` is not installed.

Wheel availability, checked on PyPI: `surrealdb` publishes
`cp39-abi3-manylinux_2_17_aarch64` (abi3, so one wheel serves any
CPython >= 3.9). Its deps are `aiohttp`, `pydantic-core`, `requests`,
`websockets` — of those, `aiohttp` and `pydantic-core` ship **per-minor**
wheels (no abi3), so they need `cp314` builds to exist for Python 3.14.
If pip reports no matching distribution for one of those two, the
fallback is Python 3.13 from the same repo — **not** compiling
`pydantic-core`, which is Rust.

Note: the `3.0.0b*` client betas are **sdist-only, zero wheels**, so
`2.0.0` is the only version of this client that installs on aarch64 at
all. There is no client-side escape hatch if 2.0.0 turns out to be
incompatible with server 3.3.3 — that would make the server pin
mandatory rather than advisable.

```sh
/usr/bin/python3 - <<'PY'
import asyncio
from surrealdb import AsyncSurreal

async def main():
    db = AsyncSurreal("ws://127.0.0.1:8000/rpc")
    await db.signin({"username": "root", "password": "root"})
    await db.use("main", "main")
    await db.query("CREATE snb_probe:1 SET note = 'vehicle-2'")
    print(await db.query("SELECT * FROM snb_probe"))
    print("PROBE_OK")

asyncio.run(main())
PY
```

`PROBE_OK` → run v3 on-device, no v2 pin needed. An exception → pin a v2
server binary and note the incompatible call. Either result is the answer.

## 6. Stop here — report, don't continue

Do NOT install the API yet. Report:

1. `/health` status line + `/version` body
2. `free -m` with surreal running
3. `PROBE_OK`, or the exception verbatim
4. Any `WARN`/`ERROR` from the surreal side (env-var renames surface here)
## 7. Storage layout (decided 2026-10-13)

Vehicle 2 keeps everything on **internal storage** — repo, SurrealDB files
and GGUF models all under Termux's prefix. RocksDB write latency and model
mmapping both favour fast storage; SD cards and external drives are
deferred (operator decision).

**Desktop ↔ phone sync** is a stated later goal, via adb over USB or
wireless. Do not build new sync machinery for it — the project already
has file-shaped boundaries that cover most of it:

- `notebook_export/` (notes → files, frontmatter) and the vault watcher
  (files → sources) for note round-trips
- `.apkg` export for deck sharing
- SurrealDB is a single RocksDB directory, so `adb pull`/`adb push` of
  that directory (server stopped) is a whole-database transport

The one prerequisite still open: the **real** proot rootfs path. The
guessed `$PREFIX/var/lib/proot-distro/ubuntu/rootfs` does not exist — an
earlier `mkdir -p` created it as a phantom tree. Locate it from Termux
before relying on any path:

```sh
find "$PREFIX/var/lib/proot-distro" -maxdepth 4 -name "snb-data" 2>/dev/null
```

**Resolved (2026-10-13):** the real guest root is

```
$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs
```

— note the `containers/` level, which is what defeated an earlier
`-maxdepth 4` search. Confirmed via RocksDB's `CURRENT` and
`MANIFEST-000009` markers at `…/rootfs/root/snb-data/test.db/`.

An earlier `mkdir -p` created a **phantom** sibling tree at
`…/proot-distro/ubuntu/rootfs/` (no `containers/`). Nothing real lives
there. Verify with `find "$PREFIX/var/lib/proot-distro/ubuntu" -type f`
— if it lists only `usr/local/share/ca-certificates/`, it is safe to
remove that `ubuntu` directory.

## 8. API install step — Python 3.12 via uv

**Why uv and not apt.** `pyproject.toml` declares
`requires-python = ">=3.11,<3.13"` (the upper bound exists because
langchain/langgraph 1.x, pydantic 2.9 and numpy 2.4 predate 3.13).
Ubuntu 26.04 resolute — the only Ubuntu proot-distro currently ships —
has **no 3.11/3.12/3.13 in the archive at all** (verified against
`ubuntu:26.04` on the dev box: only `3.14.4-1ubuntu0.2`). So there is no
apt route to a compatible interpreter. Options rejected:

- deadsnakes PPA for resolute — may not carry 3.12 yet, and needs a GPG
  key over a network that already fights apt.
- Re-prooting to Ubuntu 24.04 / Debian 12 (which do carry 3.12 / 3.11) —
  costs a second proot distro and redoing the SurrealDB + client install.
- Widening `requires-python` to allow 3.14 — changes the fork's support
  matrix and would need the full test battery re-run on 3.14 first.

uv is already this project's package manager (`.python-version` = `3.12`,
`Dockerfile` line 62 runs `uv sync --frozen --no-dev`), and
`uv python install` fetches a python-build-standalone CPython for
aarch64. No support-matrix change, and the install path matches CI
exactly.

### 8a. Prerequisites (git is not installed yet)

```sh
apt-get install -y --no-install-recommends git curl
git --version
/usr/bin/python3 -m pip install --break-system-packages uv
uv --version
```

The `uv` binary is standalone, so installing it with Python 3.14 is fine —
it is only used to *fetch* 3.12.

### 8b. Fetch CPython 3.12

```sh
uv python install 3.12
uv python list | head -5
```

Expect a `cpython-3.12.x-aarch64-unknown-linux-gnu` entry. This is the
step most likely to fail (new download source); stop and paste the error
if it does.

### 8c. Clone and sync (only after 8b succeeds)

```sh
mkdir -p /root/app
cd /root/app
git clone https://github.com/sudo-senpai-offx/open-notebook-plusplus.git
cd open-notebook-plusplus
uv sync --frozen --no-dev
```

`.python-version` (3.12) makes uv select the right interpreter and create
`.venv` on its own. `--frozen` installs exactly `uv.lock`, `--no-dev`
skips pytest/mypy/ruff — same as the Docker build.

Note: the public fork may lag local commits until they are pushed (see
`plusplus/main`). Everything up to N2b is published; model-dir
decoupling, the arm64 Dockerfile and docs may not be. Re-clone or
`git pull` once the push backlog clears.

**If `uv sync` starts compiling something**, stop and paste the package
name — the guest has no compiler toolchain, and the fix is either a
missing aarch64 wheel (needs a decision) or installing
`build-essential`/`cmake` (~400 MB). Do not let it silently fall back to
a Rust build the way pip did with `surrealdb`.


## 9. Boot the API on-device

**Confirmed:** `uv sync --frozen --no-dev` installed **195 packages into
864 MB** of `.venv` in 53 s, all with aarch64 wheels except two trivial
pure-Python sdists (`langdetect`, `odfpy`). No torch, transformers or
sentence-transformers — no local embedding model to carry. Imports of
`fastapi`, `surrealdb`, `fsrs`, `genanki` verified.

Two proot/gotchas that cost time, both recurring themes:

- **uv hardlinks fail under proot** (`Operation not permitted`). Export
  `UV_LINK_MODE=copy` in every shell that runs uv, or every sync dies on
  `error: Failed to install: ... Operation not permitted (os error 1)`.
- **Do not `rm -rf .venv` to "clean" a failed sync.** The venv's
  interpreter is the only thing satisfying `.python-version` 3.12; delete
  it and uv cannot find a 3.12 (guest has 3.14, Termux has 3.13, and
  `uv python install` fails — see below). Re-point instead:
  `export UV_PYTHON=/opt/python/bin/python3.12`.

**`uv python install` does not work inside proot.** It reports
`No download found for request: cpython-3.12-linux-aarch64-none` — the
`-none` means uv classified the target as having no libc, i.e. it read
the Android environment rather than the guest's glibc. Same misdetection
makes it list Termux's interpreters as `linux-aarch64-none`. Workaround
(worked): fetch python-build-standalone directly. Verified current asset
(2026-10-13): `cpython-3.12.15+20261009-aarch64-unknown-linux-gnu-install_only.tar.gz`,
49.9 MB. Extract to `/opt/python`, then symlink
`/usr/local/bin/python3.12`. `uv venv`/`uv sync` then accept it fine —
only the download-URL construction was broken.

### 9a. `.env`

Not committed (gitignored). Generate a **fresh** encryption key
on-device — never copy the dev box's, which is a live secret:

```sh
cd /root/app/open-notebook-plusplus
umask 077
OPEN_NOTEBOOK_ENCRYPTION_KEY=$(/opt/python/bin/python3.12 -c 'import secrets;print(secrets.token_urlsafe(32))')
printf '%s\n' \
  "OPEN_NOTEBOOK_ENCRYPTION_KEY=$OPEN_NOTEBOOK_ENCRYPTION_KEY" \
  'SURREAL_URL=ws://127.0.0.1:8000/rpc' \
  'SURREAL_USER=root' \
  'SURREAL_PASSWORD=root' \
  'SURREAL_NAMESPACE=open_notebook' \
  'SURREAL_DATABASE=open_notebook' \
  'AUTO_MODEL_POLICY=offline-first' \
  'CLOUD_ALLOWED=false' \
  'PREFER_LOCAL=true' \
  'DEGRADE_GRACEFULLY=true' \
  'TIME_BUDGETS_ENFORCED=true' \
  'API_HOST=0.0.0.0' \
  'MINI_ONLY=true' \
  'OPEN_NOTEBOOK_WORKER_MAX_TASKS=1' \
  'OPEN_NOTEBOOK_EMBEDDING_BATCH_SIZE=8' > .env
chmod 600 .env
```

`MINI_ONLY` already defaults to true (`open_notebook/ai/model_policy.py:42`);
stated explicitly so the phone's intent is visible in one place. Batch
size 8 matches the B1 compose alignment (the code default is 50).

### 9b. Run API + worker

`supervisor` is **not** in the venv (the Dockerfile pip-installs it
separately), so run the two programs directly — matching
`supervisord.conf`'s commands minus the `uv run` wrapper:

```sh
nohup .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 5055 > /root/api.log 2>&1 &
nohup .venv/bin/surreal-commands-worker --import-modules commands --max-tasks 1 > /root/worker.log 2>&1 &
sleep 20
curl -s http://127.0.0.1:5055/health; echo
```

`--host 0.0.0.0` so the phone browser (and later the LAN PWA) can reach
it, not just loopback.

### 9c. A failed `uv sync` poisons the uv cache (root cause)

Symptom: a package directory exists but has no `__init__.py`, so imports
fail with the misleading

```
ImportError: cannot import name 'submit_command' from 'surreal_commands' (unknown location)
```

`unknown location` is the tell — it means "directory found, no
`__init__.py`" (namespace package), not "package missing".

**Mechanism (confirmed 2026-10-13).** uv unpacks a wheel once into
`~/.cache/uv/archive-v0/<hash>/`, then links/copies from that cache into
each venv. The first sync died on the proot hardlink failure *while
populating that cache*, leaving a permanently incomplete cache entry.
Every later sync re-copied the same truncated tree — `rm -rf .venv` plus
a full re-sync reproduced the identical partial install, which is what
ruled out a one-off interruption.

Observed: `surreal_commands/` held 4 entries (`cli`, `core`,
`decorators.py`, `repository/`) instead of 19 files. Fix:

```sh
uv cache clean surreal-commands
UV_LINK_MODE=copy UV_PYTHON=/opt/python/bin/python3.12 \
  uv pip install --python .venv/bin/python \
  --reinstall-package surreal-commands surreal-commands==1.3.1
find .venv/lib/python3.12/site-packages/surreal_commands -type f | wc -l
```

Expect the real file count (19, excluding `__pycache__`). Escalate to
`uv cache clean` (whole cache, re-downloads everything) if a targeted
clean does not change the number.

**Deleting `.venv` alone never fixes this** — it was a wasted ~2 min
rebuild here. Reach for the cache.

**Verify completeness authoritatively**, not by importing. Each wheel
ships a `dist-info/RECORD` listing exactly what it installed, so a
missing file is proof of a partial unpack even for packages nothing
imports:

```sh
cd .venv/lib/python3.12/site-packages
for r in *.dist-info/RECORD; do
  while IFS=, read -r f _; do
    case "$f" in ../../../*) continue;; esac
    [ -e "$f" ] || echo "MISSING $f"
  done < "$r"
done | head -30
```

No output means all 195 packages are intact.

Note the `rm -rf .venv` cleanup below is only safe *with*
`UV_PYTHON` re-exported; without it, deleting the venv also removes the
only 3.12 interpreter (see 9).

### 9d. Repair loop: derive owners from RECORD, never from a hand-written list

The truncated-hardlink casualties are the files that were *in flight*
when uv aborted — always `__init__.py` in sub-directories, because those
are the small files being linked at that moment. Confirmed set on
SD680: `surreal_commands` (its root `__init__.py`), then
`cyclopts/docs`, `esperanto/providers`, `langgraph/cache/redis`,
`numpy/_pyinstaller`, `openai/types/beta/chat`,
`podcast_creator/resources/streamlit_app/utils`.

**A missing file's owning distribution is not guessable from its path.**
`langgraph/cache/redis/__init__.py` belongs to `langgraph-checkpoint`,
not `langgraph` — four distributions share the `langgraph/` namespace
tree (`grep -l "^langgraph/" *.dist-info/RECORD` on the dev box returns
four). Repairing by a list of package names written from filenames left
this one broken across two attempts.

Derive the owners, then clean and reinstall exactly those:

```sh
cd /root/app/open-notebook-plusplus/.venv/lib/python3.12/site-packages
DISTRO=$(for r in *.dist-info/RECORD; do
  while IFS=, read -r f _; do
    case "$f" in ../../../*) continue;; esac
    [ -e "$f" ] || echo "${r%%.dist-info/RECORD}"
  done < "$r"
done | sort -u)
echo "dists owning missing files: $DISTRO"
cd /root/app/open-notebook-plusplus
export UV_LINK_MODE=copy UV_PYTHON=/opt/python/bin/python3.12
uv cache clean $DISTRO
for p in $DISTRO; do uv sync --frozen --no-dev --reinstall-package "$p"; done
```

Run it as **one pasted block** — `$DISTRO` assigned in one command and
consumed in another silently expands to nothing when a paste is split,
which looks like "the repair ran and did nothing".

To settle "install dropped it" vs "RECORD over-reports", inspect the
wheel directly:

```sh
.venv/bin/python -m pip download --no-deps -d /tmp/w langgraph-checkpoint==4.2.0
.venv/bin/python -c "import glob,zipfile;w=glob.glob('/tmp/w/*.whl')[0];print([n for n in zipfile.ZipFile(w).namelist() if 'cache/redis' in n] or 'NOT IN WHEEL')"
```

**Every affected package imported cleanly the whole time.** `langgraph`'s
5 KB Redis cache module was simply absent and nothing noticed; the first
person to configure a Redis checkpointer would have hit an unexplained
`ImportError`. Import smoke tests cannot detect this class of damage —
the RECORD sweep is the only complete check, and it costs ~20 s.

## 10. CORRECTION — v3 speaks the protocol but cannot run our schema

Section 5 above concluded "no server pin is required". **That was
over-generalised and is now falsified.** The `PROBE_OK` probe covered
`signin` / `use` / `CREATE` / `SELECT` — plain CRUD. The app's first
real statement is a *migration*, and v3 rejects our SurrealQL:

```
RuntimeError: Failed to run database migrations:
Parse error: FLEXIBLE must be specified after TYPE
1 | ...LE source FLEXIBLE TYPE option<object>; DEFINE FIELD IF NOT EXISTS title O...
```

SurrealDB **v3 changed the grammar**: `FLEXIBLE` must now follow `TYPE`.
Our migrations are v2-era, so the API dies at startup (`main.py:226`)
and the worker then reports `NotFoundError: The table 'command' does not
exist` — a symptom, not a cause.

Distinguish the two layers, which a single probe conflated:

| Layer | v3.3.3 + client 2.0.0 |
|---|---|
| RPC transport (`/rpc`, auth, `use`, CRUD) | works — `PROBE_OK` |
| SurrealQL dialect (schema DDL) | **breaks** — migrations unrunnable |

So the v3 question is settled in the opposite direction: **pin v2.**
This is a stronger reason than the data-directory migration argument
recorded in `research-surreal-lowmem.md` — that one was "unsafe to
upgrade", this one is "cannot run at all". Porting the migrations to v3
syntax is a real project, not a config flip.

### 10a. Install the v2 server on-device

Release verified on GitHub: `v2.7.0` (2026-09-28), asset
`surreal-v2.7.0.linux-arm64.tgz`, 23.2 MB (note `arm64`, not
`aarch64`, in the asset name).

```sh
# stop the v3 server first (Ctrl+C in its Termux session) — it holds :8000
mv /usr/local/bin/surreal /usr/local/bin/surreal-v3
cd /tmp
curl -fSL --retry 5 --retry-all-errors -o s2.tgz \
  'https://github.com/surrealdb/surrealdb/releases/download/v2.7.0/surreal-v2.7.0.linux-arm64.tgz'
tar xzf s2.tgz && mv surreal /usr/local/bin/surreal
surreal version   # expect 2.7.0 for linux on aarch64
```

Start on a **fresh** data dir — the existing store was written by v3 and
records `version=3.3.3` in its metadata:

```sh
mv ~/snb-data ~/snb-data-v3      # keeps the v3 store; only holds snb_probe
mkdir -p ~/snb-data
SURREAL_ROCKSDB_BLOCK_CACHE_SIZE=33554432 SURREAL_HNSW_CACHE_SIZE=33554432 \
SURREAL_ROCKSDB_WRITE_BUFFER_SIZE=16777216 SURREAL_ROCKSDB_MAX_WRITE_BUFFER_NUMBER=2 \
SURREAL_ROCKSDB_JOBS_COUNT=2 SURREAL_ROCKSDB_THREAD_COUNT=1 \
surreal start --user root --pass root rocksdb:~/snb-data/test.db
```

Then restart the API and worker. Both the v2 and v3 binaries stay on the
device (`surreal`, `surreal-v3`) so the dialect difference can be
re-tested later without another download.

### 10b. Confirmed: API healthy on v2.7.0

```
$ curl -s http://127.0.0.1:5055/health
{"status":"healthy"}
```

Migrations ran clean on the v2.7.0 server — the v3 `FLEXIBLE must be
specified after TYPE` error is gone, which is the positive
confirmation that the failure was dialect, not transport. Envelope
honoured on v2: `Block cache size: 33554432`, `Background thread count:
1`, `Maximum background jobs count: 2`, `Write buffer size: 16777216`.
Note v2 prints `Block cache size` where v3 printed a `Memory manager:
total memory limit` pair — don't read the absence of v3's line as the
knob being ignored.

Vehicle 2 backend status: SurrealDB v2.7.0 + worker + FastAPI in proot,
`llama-server` native in Termux. Remaining: no UI (the Next.js
standalone bundle is a separate step, and there is no browser-facing
surface without it).

## 11. UI access — dev-box frontend, phone as client (route A)

Network topology confirmed: dev box `10.7.105.213` (wlo1, `10.7.96.0/19`)
and phone `10.7.112.85` — same subnet. Dev box → phone API answered
`/health` in 155 ms.

**Getting the phone's IP on Android is not a one-liner.** `ifconfig` fails
(`/proc/net/dev: Permission denied`, blocked since Android 10),
`iproute2` fails (`Cannot bind netlink socket: Permission denied`), and
`getprop dhcp.wlan0.ipaddress` is empty on modern Android. What works:

```sh
.venv/bin/python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.connect(('10.7.96.1',80));print(s.getsockname()[0])"
```

A UDP `connect()` sends nothing; the kernel just answers "which local
address would I use for this route". No `/proc`, no netlink, no root.

### 11a. `INTERNAL_API_URL` is a BUILD-time variable for standalone

`next.config.ts` reads `process.env.INTERNAL_API_URL` inside
`rewrites()`. For `output: "standalone"` that function is evaluated
during `next build` and serialised into the routes manifest — setting it
when `server.js` starts has **no effect**. Symptom when you get it wrong:

```
Error: connect ECONNREFUSED ::1:5055 ... 127.0.0.1:5055
```

i.e. HTTP 500 on `/api/*` while the pages themselves render fine.
Invisible in Docker because the default `http://localhost:5055` is
correct for a single container — only cross-host setups hit it.

```sh
cd frontend && INTERNAL_API_URL=http://<phone-ip>:5055 npm run build
```

### 11b. Assemble and run

Standalone output alone does not serve assets. Mirror the Dockerfile
(`Dockerfile:117-120`): copy `standalone/` to a run dir, then place
`.next/static` and `public/` as its siblings.

```sh
rm -rf /tmp/next-run && mkdir -p /tmp/next-run
cp -r frontend/.next/standalone/. /tmp/next-run/
mkdir -p /tmp/next-run/.next
cp -r frontend/.next/static /tmp/next-run/.next/static
cp -r frontend/public /tmp/next-run/public
cd /tmp/next-run && PORT=8502 HOSTNAME=0.0.0.0 node server.js
```

`HOSTNAME=0.0.0.0` is required — standalone binds loopback by default,
so the phone cannot reach it. Artifact total **65 MB** (59 MB standalone
+ 6.1 MB static + public), which is also what route B transfers to the
phone.

## 12. Route B — all-on-device UI (COMPLETE)

Route A is dead on this network: phone -> dev-box TCP is blocked while
ICMP is permitted.

| Test from phone (Termux) | Result |
|---|---|
| `ping -c2 10.7.105.213` | 2/2 replies, 60-82 ms, TTL 64 |
| `curl http://10.7.105.213:8502/` | `code=000`, `remote=` empty, **0.04 s** |
| arrivals seen by dev box | **zero** connections |

A 40 ms failure with no SYN received is an immediate `EHOSTUNREACH`,
not a timeout — the kernel has no route for TCP while ICMP goes via the
gateway. Same `/19` on paper, different forwarding policy. Do not assume
same-subnet implies mutual reachability.

### 12a. Transfer over USB adb (works regardless of network policy)

Dev box already had `adb 1.0.41` and the phone enumerated
(`CPH2333T2`). Push to shared storage, then copy into Termux:

```sh
# dev box
adb push /tmp/next-run.tar.gz /sdcard/Download/
adb shell 'md5sum /sdcard/Download/next-run.tar.gz'   # verify before copying
# phone (Termux)
termux-setup-storage      # answer y, then grant the permission dialog
cp /sdcard/Download/next-run.tar.gz ~/
md5sum ~/next-run.tar.gz
```

Artifact `21,992,680` bytes compressed, md5
`315d4667c30f8b1ec4dfa242badcd527`. **Verify on both sides** — the first
attempt left a 0-byte file (`d41d8cd9...`) from an aborted `nc -l`
redirect, which looked like a transfer failure but was a stale artifact.

### 12b. Node without installing Node

`nodejs-wheel-binaries==24.19.0` (a transitive dep of `nodeenv`) ships a
`manylinux_2_28_aarch64` wheel containing a real Node **24.19.0**, already
installed by `uv sync`. No `apt install nodejs` (~50 MB) required:

```sh
NODE=/root/app/open-notebook-plusplus/.venv/lib/python3.12/site-packages/nodejs_wheel/bin/node
$NODE -v    # v24.19.0 on aarch64
```

### 12c. Run

```sh
tar xzf /data/data/com.termux/files/home/next-run.tar.gz -C /root/frontend --strip-components=1
cd /root/frontend
PORT=8502 HOSTNAME=0.0.0.0 $NODE server.js &
```

Verified on-device: `GET /` -> 307 -> `/notebooks`, and
`GET /api/metrics/summary` returns real plusplus schema data through
Next -> rewrite proxy -> FastAPI -> SurrealDB, all on the phone. The
build-time `INTERNAL_API_URL=http://10.7.112.85:5055` resolves to the
phone itself (uvicorn binds `0.0.0.0`), so no rebuild was needed.

### 12d. Vehicle 2 process inventory (SD680)

| Process | Where | RSS |
|---|---|---|
| SurrealDB 2.7.0 | proot | 107 MB |
| FastAPI (uvicorn) | proot | 161 MB |
| surreal-commands-worker | proot | 139 MB |
| llama-server | Termux native | 93 MB |
| Next.js 16.2.6 | proot (venv Node) | not yet measured |
| **subtotal** | | **~500 MB** |

Against a 2 GB floor this leaves genuine headroom, which is the real
finding: **a complete notebook OS — LLM server, database, API, worker and
UI — runs in ~500 MB resident on an 8 GB phone.** The 3 GB SD439 class
is therefore not obviously excluded, though the llama.cpp figure there
was extrapolated, never measured.

Remaining gap: no LLM provider is registered in the app, so chat does
not work yet. llama-server is up on `:8080` but the app needs an
OpenAI-compatible provider entry pointing at `http://127.0.0.1:8080/v1`.

## 13. Chat on-device — WORKING (verified end to end)

All of this was driven from the dev box over LAN (`10.7.112.85:5055`);
only the stack start was manual.

| Step | Result |
|---|---|
| `openai_compatible` provider | `available` — env vars reached the process |
| `POST /api/models/sync/openai_compatible` | `{"discovered":1,"new":1}` |
| Discovered model | `qwen2.5-0.5b-instruct-q2_k.gguf`, auto-classified `language` |
| `PUT /api/models/defaults` | `default_chat_model=model:vvrisguqo85lhcsv4ous` |
| `POST /api/models/{id}/test` | `{"success":true,"message":"Response: Hello!..."}` |
| `POST /api/chat/execute` | coherent answer on spaced repetition |

Chat response, via the app's own langgraph pipeline:

> **Q** In one sentence, what is spaced repetition?
> **A** Spaced repetition is a learning technique that involves spaced out
> the presentation of information over a longer time period, rather than
> cramming it all into a single session.

**Latency: 62 s per turn** at `-t 2` with a 0.5B model. That is the real
cost of phone-local chat, not a defect. Thread count and model size are the
levers.

### 13a. Configuration facts worth not rediscovering

- `default_chat_model` takes a **record id** (`model:…`), not the model
  name — `ModelManager.get_model()` calls `Model.get(model_id)`.
- Model name is the **full GGUF path**, and `/v1/models` is the only source
  of that string.
- `POST /api/credentials` stores provider config in the DB (encrypted).
  `provision_provider_keys` copies DB → env at boot, so DB config survives
  a lost `.env`. This clears the "API key migration available" banner.
- Linking a model to a credential is **UI-only**: there is no `PUT
  /api/models/{id}` (405). An unlinked model falls back to env config,
  which is why the test still passed.
- **`openai_compatible` supports only `["language"]`** per the app's own
  provider registry. So llama-server cannot serve embeddings here, and
  `default_embedding_model` stays null. Consequence: **sources cannot be
  ingested on the phone yet** — chat works, RAG does not.

### 13b. What works in the UI now

Verified by screenshot: responsive hamburger drawer, notebooks list, the
API-created notebook visible in the UI, notebook page with
Sources/Notes/Chat tabs, and **Chrome offering the PWA install prompt**
(`Open Notebook PlusPlus をインストールします`) — the manifest + service
worker slice is doing its job.
