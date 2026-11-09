# Syncthing setup for Open Notebook++ (Track B, E18 P1)

## Overview

Syncthing provides **continuous, bidirectional, relay-free** sync of the
manifest directory (`$SYNC_DIR`, i.e. `OPEN_NOTEBOOK_SYNC_DIR`) between two
Open Notebook++ instances. It is an alternative to the on-demand
`rsync-sync.sh` driver — use one or the other, not both simultaneously.

The API owns all export/import semantics, conflict detection, and
last-write-wins resolution. Syncthing only moves the manifest files. Blob
bytes (PDFs, transcripts) travel on the existing HTTP channel
(`/api/sync/blobs/*`), never through Syncthing.

## Files

| File | Purpose |
|------|---------|
| `config.xml` | Syncthing configuration template — edit `DEVICE_ID_SELF`, `DEVICE_ID_PEER`, `$SYNC_DIR_ABSOLUTE`, `API_KEY` |
| `.stignore` | Patterns excluded from sync (`.sync-rc`, `.git/`, `*.tmp`, baseline/watermark) |
| `README.md` | This file |

## Setup

### 1. Install Syncthing

Install Syncthing on both devices (PC and phone). This repo does not bundle
the binary — it is an operator-provided dependency.

### 2. Generate device IDs

On each device, run:

```sh
syncthing -device-id
```

This prints the device ID (a base32-encoded ed25519 public key).

### 3. Edit `config.xml`

Replace these placeholders:

| Placeholder | Value |
|-------------|-------|
| `DEVICE_ID_SELF` | This device's Syncthing ID |
| `DEVICE_ID_PEER` | The other device's Syncthing ID |
| `$SYNC_DIR_ABSOLUTE` | Absolute path to `OPEN_NOTEBOOK_SYNC_DIR` |
| `API_KEY` | Any random string (used for REST API auth) |

Copy the edited `config.xml` to Syncthing's config directory on each device
(e.g. `~/.config/syncthing/config.xml` on Linux).

### 4. Start Syncthing

```sh
syncthing -home ~/.config/syncthing
```

The GUI is at `http://127.0.0.1:8384` (localhost only by design — see
`config.xml`).

### 5. Pair devices

In each device's GUI, add the other device by ID. Then add the folder
`open-notebook-sync` on both sides, sharing it with the peer's device ID.
Folder type must be **Send & Receive** on both devices.

### 6. Sync the manifest

Before Syncthing can sync anything, the API must have exported the manifest:

```sh
curl -X POST http://localhost:5055/api/sync/export \
  -H "Authorization: Bearer $OPEN_NOTEBOOK_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"device":"pc"}'
```

Then Syncthing will replicate the changed files within its rescan interval
(default 300s). To force an immediate rescan:

```sh
curl -X POST -H "X-API-Key: API_KEY" \
  http://127.0.0.1:8384/rest/db/scan?folder=open-notebook-sync
```

After the manifest has arrived on the peer, import it:

```sh
curl -X POST http://peer:5055/api/sync/import \
  -H "Authorization: Bearer $OPEN_NOTEBOOK_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"dry_run":false}'
```

## Why no relay server

The spec (E18) prohibits a relay server. `config.xml` sets
`<relayServer>default</relayServer>` but with `<acceptedProtocols>syncthing,local</acceptedProtocols>`
relay is only used as a last resort — on a LAN, local discovery handles it.
If you want to guarantee no relay, set `<relayServer>disabled</relayServer>`.

## Atomic write safety

`export_all` in `open_notebook/sync/exchange.py` writes each manifest file
atomically (`.tmp` then `rename`). Syncthing only sees complete files, so
there is no risk of syncing a half-written JSON file.

## Relationship to `rsync-sync.sh`

`scripts/rsync-sync.sh` is operator-initiated and point-to-point over SSH.
Syncthing is continuous and backgrounded. They are alternative transports
for the same manifest directory — pick one. Do not run both against the
same `$SYNC_DIR` at the same time.
