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

## 5. Client/server compatibility probe (decides v2-vs-v3 pin)

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