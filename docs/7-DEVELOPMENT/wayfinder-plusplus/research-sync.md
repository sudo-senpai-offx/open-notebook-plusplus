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
---

## 9. Verification against a real database — three bugs the unit tests could not find

The 22 sync tests all stub `repo_query` and patch `save()`. Running
`import_all` against the dev box's real SurrealDB 2.7.0 found three
defects in a row. This is the same lesson as the FSRS and export bugs:
**the stub is where the bug hides.**

### Bug A — importing created nothing

`ObjectModel.save()` branches on `self.id is None`. Sync sets the other
device's id, so every record took the **UPDATE** path. SurrealDB's
`UPDATE` against a record that does not exist is a no-op returning `[]`,
so `save()` then raised `IndexError` on `result_list[0]`.

Every first-time sync failed. No test caught it: they patch `save()`
outright, so the create/update branch was never executed.

`repo_create` cannot cover it either — it pops `id`. Fixed with
`_create_with_id()`, which issues `UPSERT $rid MERGE $data RETURN AFTER`:
UPSERT rather than CREATE so a concurrent import from the other device
cannot turn a duplicate into an error.

### Bug B — imported reviews were timestamped at import time (migration 34)

Migration 32 declared:

```sql
DEFINE FIELD reviewed_at ON revlog DEFAULT time::now() VALUE $before OR time::now();
```

A field's `VALUE` clause is evaluated **after** the statement's input and
replaces it. Measured:

```
CREATE revlog SET reviewed_at = '2020-01-01T00:00:00+00:00'
  -> stored: 2026-10-13T11:43:23   (the insert time)
```

The app's own writes were never affected — `record_grade()` already passes
`time::now()`. Only importers break. Two consequences:

1. **Review history is falsified.** "What did I study last week" becomes
   "what did I import last week", and any future replay of the log reads
   wrong intervals.
2. **Dedupe can never match**, because every import has a unique
   timestamp. The revlog grows without bound on every sync — observed
   going 2 → 4 → 6 rows across three imports.

Migration 34 drops the `VALUE` clause and keeps `DEFAULT`, so a supplied
value wins. `$before` had nothing to preserve: revlog rows are
append-only and never updated.

### Bug C — space-separated timestamps rejected once B was fixed

Fixing B let the supplied value through, and it then hit the field's
strict type:

```
Found '2026-10-13 06:55:36.740943+00:00' for field `reviewed_at`,
but expected a datetime
```

SurrealDB serialises datetimes with a space separator. `_as_iso()` parses
and re-emits ISO-8601. Unparseable values pass through untouched, so a
malformed row surfaces as a schema error naming the value rather than
vanishing.

### Result after all three

```
FIRST (empty db)      cards=3 notes=1 revlog=2   created=1/3  revlog+=2
SECOND (same input)  cards=3 notes=1 revlog=2   stale=1/3    revlog+=0 dup=2
THIRD (as 2nd device) cards=3 notes=1 revlog=2  stale=1/3    revlog+=0 dup=2

graded card: stability=2.3065 state=1 due=2026-10-13T07:13:55
revlog:      06:55:36, 07:03:55  <- real review times
```

Idempotent, lossless, and the review log no longer grows.

### A test that broke for the right reason

`test_migration_33_is_registered` asserted on `up_migrations[-1]` and
failed the moment migration 34 landed. It now searches the whole list, so
migration 35 will not require editing it.

---

## 10. Churn and silent loss — migration 35

Found by checking *when* the phone's records claimed to change after a
pull, rather than only *whether* they changed:

```
repo file   updated = 2026-10-14T09:00:00   (the PC's edit)
phone DB    updated = 2026-10-13T17:10:04   (the import moment)
```

### Cause

Same shape as migration 34, one layer over:

```sql
DEFINE FIELD updated ON flashcard DEFAULT time::now() VALUE time::now();
```

The `VALUE` clause replaces whatever the statement supplied, so **no
write could ever set `updated` to anything but the moment of the write**.

Two costs:

1. **Guaranteed churn.** The next export wrote the phone's own `updated`,
   so every push/pull cycle produced two commits flipping it back and
   forth.
2. **Silent loss under clock skew.** Sync resolves conflicts by comparing
   `updated`. A record imported at *T* looks newer than any edit made
   before *T*, so a PC edit timestamped *T−1h* is skipped as "stale" and
   dropped without a message.

### Two fixes were needed, not one

Migration 35 removed the `VALUE` clause — and on its own changed
**nothing**, because the importer was never *supplying* a timestamp, only
reading one for the last-write-wins comparison. The schema was enabled
for a value nobody sent.

`_apply_timestamp()` closes that half. It runs after the content write
because `ObjectModel.save()` unconditionally stamps `updated` with the
current time, so the incoming value has to be restored afterwards. Two
writes per imported record; import is not a hot path.

### Scope, deliberately narrow

Only `flashcard` and `note` — the two tables sync owns. `source`,
`episode`, `notebook`, `transformation`, `focus_session`, `site` and
`revlog` keep the existing definition, because their raw writers are
mostly embedding updates and some callers may reasonably expect
`updated` to move. `episode_profile` and `speaker_profile` already
declare `TYPE datetime DEFAULT time::now()` with no `VALUE` clause, so
this brings two tables in line with existing practice rather than
inventing one.

Safe because every model write supplies the field: `ObjectModel.save()`,
`repo_create` and `repo_update` all set it explicitly. The only raw
writer of these tables is `UPDATE $record_id SET embedding = $embedding`
(`commands/embedding_commands.py:143`), which sets only the embedding —
and an embedding write should not move a note's content age anyway.

### The last churn came from two fields of my own

With `updated` stable, the only diffs left were `source_id` and
`written_by`:

- `source_id` is deliberately not imported, so the phone always exports
  `null` and the PC always exports its real id.
- `written_by` is stamped with the exporting device, so the two devices
  overwrote each other on every cycle.

Both are now dropped from the payload. `source_id` has no meaning in a
file where sources do not sync, and provenance already lives in the git
commit author and message. Keeping either produced a commit per cycle,
which is churn by another name.

### Verified

```
import -> export, unedited records: IDENTICAL — zero churn, nothing to commit
imported timestamp preserved:        db 2026-10-12 09:00:00 == file
local edit still bumps updated:      2026-10-13 17:14:09
```

A local edit *should* change the file. That is content, not churn.

---

## 11. Notebooks on import — resolved by slug

A synced note used to arrive unattached from every notebook, which made
working sync feel broken. Record ids cannot carry that meaning: they are
per-database, so `notebook:89ihirkrx...` on the phone names nothing on
the PC.

### Export

One query each, not one per note:

```sql
SELECT id, name FROM notebook
SELECT in, out FROM artifact          -- note -> notebook edges
```

Each note payload gains `notebook_slugs`, and `notebooks/index.json`
lists `{slug, name}` for everything known. `_slug()` lowercases, collapses
non-alphanumerics to `-`, and trims — so `"Cognitive Science"` and
`"cognitive science"` resolve to the same notebook.

### Import

Build `{slug: local_id}` once, then relate only to notebooks that exist
here. Result counters:

| Counter | Meaning |
|---|---|
| `notes_attached` | linked to at least one local notebook |
| `notes_unattached` | imported, but no local notebook matched |
| `missing_notebooks` | the slugs that did not resolve |

### Verified across two real databases

Device A and device B each had a notebook named "Cognitive Science" with
**different record ids**:

```
device A note -> notebook:k13atwmrlolgrpwflgg8
device B notebook          = notebook:q80innpk4w1mw91wg48u
import: attached=1 unattached=0 missing=[]
edge: note:8r68iczcl5i1wmajc8sx -> notebook:q80innpk4w1mw91wg48u
attached to device B's notebook? YES
```

And the failure case, on a device with no notebooks at all:

```
attached=0  unattached=1  missing=['cognitive-science']
artifact edges created: 0        <- no dangling reference
notes still imported:  1         <- content preserved
```

The second result matters most. SurrealDB record links do **not** prevent
dangling references, so relating a note to a notebook id that does not
exist locally would leave a broken edge that nothing downstream
complains about. Import only relates to notebooks it has confirmed, and
names the ones it could not resolve instead of failing or pretending.

Notebooks themselves are not synced. Creating them on import was
considered and rejected: it litters a device with folders the operator
never asked for, and a name is not consent to make one. The report tells
you what is missing so you can create it deliberately.
