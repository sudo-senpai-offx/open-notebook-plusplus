# Sync: two Open Notebook instances, one phone and one PC

Status: **core format + export/import implemented and tested. Transport not yet written.**

## 1. The constraint that picks the transport

Phone → PC TCP is **blocked** on the operator's network. ICMP succeeds;
`curl` to the PC fails in 0.04 s with **zero SYNs arriving** at the
destination. PC → phone works. This is not a configuration problem and
will not be fixed by a setting, so it eliminates every design where the
phone dials out:

| Rejected | Why |
|---|---|
| Live sync service on the PC, phone as client | requires phone → PC |
| Phone-hosted server, PC as client | requires phone → PC |
| Reverse SSH tunnel | requires phone → PC |
| mDNS / LAN discovery | the phone cannot complete a handshake to any PC address |

What works, established this session:

- **Phone has outbound HTTPS.** It pulled 35 MB from Hugging Face in 5 s.
- **PC has outbound HTTPS.** It has pushed to GitHub repeatedly.
- **adb works both ways** but `adb shell` runs as the `shell` user and
  cannot read `/data/data/com.termux` (mode 0700), so nothing can be
  executed inside Termux remotely.

That leaves exactly one unattended bidirectional transport: **a git
repository both sides clone.** History for free, and a genuine conflict
becomes a visible merge conflict rather than a silent overwrite.

## 2. Three formats, deliberately not interchangeable

The single most expensive mistake available here would be to reuse the
Anki exporter for sync. It cannot work, and the failure is silent.

**Measured, not assumed.** A card with `stability=2.3065` and a real
`due` date, exported through `.apkg`, produces an Anki `cards` row with:

```
{'due': 0, 'ivl': 0, 'type': 0, 'queue': 0}
```

genanki hardcodes those four. Syncing cards as `.apkg` would reset every
card's review history on the receiving device, with nothing in the
response to indicate it.

| Format | Purpose | Lossy? |
|---|---|---|
| JSON (`sync/`) | app ↔ app sync | no — carries FSRS state and revlog |
| `.apkg` | Anki / AnkiDroid interop | **yes** — scheduling zeroed |
| Markdown + frontmatter | Obsidian vault interop | yes — no scheduling, no ids beyond one field |

## 3. What syncs, and what deliberately does not

**Notes** — full bidirectional, last-write-wins on `updated`.

**Cards** — full bidirectional: content *and* `fsrs_json`. The revlog is
an **append-only union**, deduplicated on
`(card_id, rating, reviewed_at, duration_ms)`.

**Sources** — **not synced.** They are ingested documents whose embedding
vectors live outside the record, so a JSON copy arrives with no vectors
and no way to know whether the original still exists on the other device.
Files already have a better transport (vault ingest). Syncing them here
would create orphaned embeddings and near-duplicate content. Cards
therefore drop `source_id` on import — deliberately dangling-free.

## 4. Conflict policy, including the hole

| Item | Policy | Cost |
|---|---|---|
| Note | later `updated` wins | same-second edits on both devices: one loses |
| Card content | later `updated` wins | same |
| Revlog | **union**, deduped | none — both reviews kept |
| `fsrs_json` | later `last_review` wins | see below |

Ties go to the local copy, so a re-import is never destructive.

**The acknowledged hole:** if both devices review the same card within
one second, both revlog rows survive but only one FSRS state does, so the
next interval reflects one review rather than two. Recovering would mean
replaying the merged log through FSRS — a larger change than this slice.
It loses schedule precision, not history.

`updated` is second-resolution, which is why the tie-break matters.

## 5. Layout

```
sync/                     <- git checkout, OPEN_NOTEBOOK_SYNC_DIR
  manifest.json           <- schema_version, device, counts
  notes/note-<id>.json
  cards/flashcard-<id>.json
  revlog/revlog.jsonl     <- append-only
```

Filenames derive from the record id, so two devices exporting the same
note produce the **same path**. That is intentional: it makes git report
a conflict rather than silently creating two files.

Writes are atomic (temp + `rename`) so a half-written file never becomes
a merge conflict on the other device.

## 6. API

| Endpoint | Purpose |
|---|---|
| `GET /api/sync/status` | where sync reads/writes, what is present |
| `POST /api/sync/export` | write everything to the sync dir (`{device}`) |
| `POST /api/sync/import` | upsert by id. **`dry_run` defaults to true** |

`dry_run` defaults on because a sync that overwrites is not something to
discover by running it.

No background daemon and no scheduler. The transport is the operator's
repository; a job pushing to it on a timer is not something to enable by
default.

## 7. Deliberate round-one exclusions

- **No delete propagation.** A deletion on one device does not delete
  elsewhere; the next export would resurrect it. Delete needs a tombstone
  with a timestamp, which is a schema change, not a script.
- **No notebooks**, so notes do not travel with their notebook grouping.
- **No embeddings.** A synced source would need re-embedding; since
  sources are not synced, only notes/cards move, and neither is chunked
  into the vector store.
- **No conflict UI.** Conflicts surface as git merge conflicts in the
  sync repo, which is a deliberate choice: the operator resolves them.

## 8. Verification

22 tests in `tests/test_sync_exchange.py`, all of which fail when the
corresponding fix is reverted:

- FSRS `stability` and `due` survive serialisation (the reason this is
  JSON, not `.apkg`)
- stale records are skipped and not written; newer records overwrite
- `dry_run` writes nothing
- revlog union adds only genuinely new rows
- **two devices' reviews of the same card both survive**
- `source_id` is not carried across
- no import of the vault module (AST-checked, not grepped — a grep
  matches the module's own docstring explaining why the vault is unused,
  which is how a test passes for the wrong reason)