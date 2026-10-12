# SD680 proot step runbook (vehicle 2 — needs hands on-device)

Goal: FastAPI + SurrealDB running inside Ubuntu proot, Termux-native
llama.cpp + PWA shell around it. Bench numbers + thermal verdict already
clear this device; this step proves the server half on-device.

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
surrealdb version
```

Paste back: the `surrealdb version` output (want the `linux-arm64` build
string) and any install errors.

## 3. Boot SurrealDB alone first (isolate variables)

```sh
mkdir -p ~/snb-data
surreal start --user root --pass root rocksdb:~/snb-data/test.db
```

From a second Termux session, check it answers, then stop it (`Ctrl+C`
in the first session):

```sh
curl -s http://127.0.0.1:8000/health; echo
```

Paste back: health output + how long boot took + `free -m` while it runs.

## 4. Stop here — report, don't continue

Do NOT install the API yet. What I need back:

1. `surrealdb version` string
2. `/health` output + boot seconds
3. `free -m` with surreal running
4. Any error text verbatim

These four decide the API step (venv size, bind address, proot networking
notes). The FastAPI install + PWA-shell wiring is the follow-up runbook.
