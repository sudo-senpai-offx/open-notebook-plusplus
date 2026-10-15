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

---

## 12. Slugs were ASCII-only, and that silently erased real notebooks

Found by exporting the operator's actual PC database rather than a test
fixture. `_slug()` used `[^a-z0-9]+`, so:

```
'日本語勉強会   ||   アドバンス'   -> ''
'Ökonomie'                        -> ''
```

An empty slug is then filtered out of `notebook_slugs` and renders as an
empty entry in the index. So the notebook disappears from sync, and every
note attached to it exports **ungrouped** — with no error anywhere. On
this account both notes happened to hang off exactly that notebook, so
2 of 2 exported with `notebook_slugs=[]`.

Now Unicode-aware: NFKC first (folds full-width to half-width so
lookalikes match), then `casefold()`, then `\W+` → `-` — `\w` is
Unicode-aware in Python 3, so CJK and accents survive. A name with no word
characters at all falls back to `notebook-<sha1[:8]>`, which is still
stable across devices and still distinct.

| Input | Slug |
|---|---|
| `日本語勉強会   \|\|   アドバンス` | `日本語勉強会-アドバンス` |
| `Ökonomie` | `ökonomie` |
| `Ａ Ｂ` | `a-b` (NFKC folds full-width) |
| `!!!` | `notebook-9a7b006d` |

`test_slug_is_never_empty` now asserts the invariant directly, because an
empty slug is indistinguishable from "no notebook" and that ambiguity is
the whole bug.

### The index is per-device, and that is a known limitation

`notebooks/index.json` is regenerated from the *exporting* device's own
notebooks, so it lists that device's notebooks rather than the union
across devices. When the PC exported, its seven notebooks replaced the
phone's single one in the file. This is informational only — resolution
uses the per-note `notebook_slugs`, which are correct on both sides. A
true union would need a merge, which git cannot do for a single
whole-file rewrite.

---

## 13. Content-addressed storage: sources were four copies of one thing

### What was actually there

Measured on the operator's PC, not assumed:

| Representation | Where | Size |
|---|---|---|
| original bytes | `data/uploads/<uuid>.pdf` | 49 MB over 258 sources |
| extracted text | `source.full_text` | — |
| the same text, chunked | `source_embedding.content` | 2,826,569 chars |
| vectors | `source_embedding.embedding` | 2,754 × 384 ≈ 4.2 MB |

The text was stored twice and the file was a third representation of
it. Editing a source regenerated all four. Nothing was shared, and
`notebook_data/.cache` added 20 GB of HF model weights on top — not
content, but another reason the data directory cannot simply be copied
to a phone.

### Why sources were excluded from sync

The earlier note said sources were excluded "because vectors live
outside the record." That was wrong: `source_embedding` holds the
vector *and* the chunk text, both inside the database. Neither should
ever sync — both are regenerable from the file plus a local embedding
model. The real blocker was that a synced source arrived with no file
behind it, because the file was identified by a local path.

### The design

`data/blobs/<ab>/<sha256><ext>`. Identity is the content hash, so:

- identical bytes occupy one path, with no lookup required — the second
  write finds the file already there;
- the hash is what crosses devices, where a path means nothing;
- processing opens the blob in place, so there is no second copy during
  extraction;
- extension is preserved because `_assert_file_supported()` and the PDF
  text-layer probe both branch on it.

Sharded two levels because a flat directory of tens of thousands of
entries is pathological on `sdcardfs`. Writes go to a temp file and are
`os.replace`d, which is atomic.

Content addressing also dissolves the TOCTOU race the old
`generate_unique_filename()` guarded: with a deterministic name, "is this
name free?" and "claim the name" are the same step. Two concurrent
uploads of identical bytes now both compute the same path and write the
same content, so whichever `replace` wins the result is correct. That
function and its dedicated test were removed rather than left as dead
code asserting a fix to a mechanism that no longer runs.

### Dedup makes delete dangerous, and two bugs followed

Two sources sharing a blob means deleting one must not unlink bytes the
other still reads. Two places did a bare `os.unlink`: `Source.delete()`
and the extraction graph's `delete_source` cleanup. Both are now
reference-aware.

The test suite passed while both were still broken, because stubs cannot
express a shared reference. Against a live SurrealDB 2.7.0:

1. **`unlink_if_unreferenced` passed the blob *path* where a *hash* was
   expected.** `is_referenced` queries `asset.sha256`, so the filter
   could never match and every blob looked unreferenced — the exact data
   loss the check existed to prevent. The hash is now a separate
   parameter.

2. **`id != $exclude` silently matches nothing when `$exclude` is a
   string.** Measured on the live server: excluding `source:one` by
   string returned `n=2`, by `RecordID` returned `n=1`. Every source
   counted as its own referrer, so blobs were *never* collected — a
   silent disk leak, failing in the direction that looks safe. Now
   coerced with `ensure_record_id()`, with a regression test asserting
   the parameter type.

A third, lower-severity issue: the reference check fails **safe**. If
the database is unreachable it reports "referenced" and keeps the file.
Leaking an orphan blob is recoverable; deleting study material during a
brief DB outage is not.

Verified end to end on a live database: two sources sharing a blob →
deleting one keeps it → deleting the last collects it → re-ingesting the
same bytes returns the same path.

### What research contributed

Two references were reachable (`websearch` returned nothing on this box
and `docs.syncthing.net` timed out):

- **Anki** keys media by *filename*, and its docs are explicit that it
  "will not notice if you have made edits to existing files" — you must
  delete and re-add to push a content change. Addressing by hash gets
  edit-detection for free. Anki also only propagates deletions once media
  is fully in sync, which is the same resurrection problem as our
  missing tombstones, solved there by sequencing.
- **CAS** gives the dedup property, and names its own limit honestly: it
  suits static documents, because every edit yields a new address. That
  is correct here — a source file is immutable once ingested; edits
  create a new source.

### Not done yet

Sources still do not sync. This step is storage only. Still outstanding:
the manifest half (hash + metadata, no bytes), the blob transfer channel,
LAN first with git as the offline-tolerant queue, and tombstones so a
delete on one device is not resurrected by the next export.

---

## 14. Selective OCR: Tesseract + pypdfium2

### Why not Docling

Docling needs PyTorch and multi-hundred-megabyte layout and table
models. On the floor device - an SDM439-class 3 GB phone already running
two llama-servers - it is not slow, it is fatal: the OOM killer takes the
app. Tesseract is ~30 MB, pure CPU, and reads a page in about a second.

`pypdfium2` was already in the venv as a transitive dependency, which
removed the other half of the problem: rasterising PDF pages needs no
system poppler, so there is no second native dependency to install.

### The part that is actually the design

OCR over a whole document is the obvious approach and the wrong one. It
is also unnecessary, because the PDF already knows which pages need
reading. Classifying a page costs only its object list, which pdfium
parsed anyway:

| | |
|---|---|
| **TEXT** | a real text layer, little imagery - already extracted, skip |
| **GRAPHICAL** | no usable text - a scan or a figure, read the page |
| **MIXED** | both - read only the regions occupied by images |

Measured across this library: **618 readable pages, 411 text, 207
graphical. 66.5% skipped.** The 196-page textbook drops from 196 pages
of recognition to 7. That ratio is the whole argument for doing this
rather than simply running OCR at ingest.

### Quality against speed

**200 DPI.** Tesseract's guidance is that x-height must reach 20px; 10pt
body text is ~14pt x-height, so 200 DPI yields ~28px, comfortably past
it. 300 DPI costs ~2.2x the pixels for no gain on clean type, and is
what the retry path is for on degraded scans.

**Confidence gating.** Tesseract scores every word. Words below 70 are
dropped; a page whose survivors still average below 60 is discarded
entirely. Without this a molecular diagram yields lines like
`KINA i N` and `\e 'protein =` - technically a result, completely wrong,
and poison to `full_text`, embeddings and search. Discarding is better
than storing, because the damage is hard to trace back later.

**One process per page, OMP_THREAD_LIMIT=1.** Tesseract spawns its own
OpenMP pool. On a device already running two llama-servers, letting it
claim every core is how the whole app gets killed. It is a subprocess by
design - linking it into the worker would carry its memory for the
lifetime of the app.

Measured on this machine: **1.2 s/page**, confidence 88-90, on the
lecture slides.

### Two bugs that failed silently, and how they were found

The first version flagged **77%** of the library for OCR and read
**nothing**. Both numbers were wrong for the same reason - the detector
counted text *objects* rather than characters, so a normal typeset page
scored 5 against a threshold of 200.

The second was worse and quieter: `int("92.167656")` raises, so every
real word was discarded by the confidence filter and the whole library
OCR'd to zero. That is indistinguishable from a filter doing its job.
Only rendering a page to PNG and looking at it - a title slide with
perfectly legible type - showed that "no text found" and "no text there"
are different claims.

A third: piping tesseract's TSV emits the confidence column but
silently empties the text column. Given an output base it writes
`<base>.tsv` correctly - 22 words at confidence 88-97 versus zero over a
pipe.

All three now have regression tests. The lesson is the one that keeps
repeating in this project: a plausible-looking zero is the most
dangerous thing a filter can return.

### Result

`Protein purification_compressed.pdf` previously failed extraction
outright with an actionable error. It now yields **10,418 characters** of
real lecture content at confidence 88, in about a minute for 42 pages.

### Configuration

    OPEN_NOTEBOOK_OCR_ENABLED=true     master switch, fails closed
    OPEN_NOTEBOOK_OCR_LANG=eng+jpn     any Tesseract language set
    OPEN_NOTEBOOK_OCR_BIN=tesseract    binary to exec
    OPEN_NOTEBOOK_OCR_DPI=200          clamped to 150-400
    OPEN_NOTEBOOK_OCR_TIMEOUT=300

---

## 15. Tombstones: making a deletion survive the round trip

### The bug

Delete a source on the phone, sync, and it comes back. The PC never
learned of the deletion, so it exported its copy again and the import
recreated the record. The user watches their own delete silently
un-happen, and the second time they lose trust in every other delete.

### Why not just diff the manifest

Because absence is not information. Git cannot tell "never exported"
from "deleted", and neither can a file listing. A deletion has to be
written down explicitly.

    tombstones/source-abc123.json
    {"id": "source:abc123", "kind": "sources",
     "updated": "2026-10-14T18:30:00+00:00", "deleted": true,
     "sha256": "9f2c..."}

### Where they come from: the export sweep

Not from the delete methods. `export_all` compares the records that
exist now against the json files the manifest already holds, and
tombstones everything that vanished - then removes the stale file so
the manifest stops carrying a record that no longer exists.

This was the deliberate choice. Poking `Source.delete()`,
`Notebook.delete()`, the notebook cascade, the UI routes and whatever
script the user writes next would cover the four paths already known
about and miss the fifth without a sound. The sweep catches every
route, including ones nobody remembered to instrument, and it lives in
one place instead of five.

### How they resolve

By the rule that was already there. `import_all` is last-write-wins on
`updated`; a tombstone is compared the same way.

  * tombstone newer than the local record -> delete it
  * local record newer than the tombstone -> ignore the tombstone

That second case is the point of the design rather than a caveat. Delete
is a write like any other, so the most recent activity wins whichever
device it happened on - and editing a source after someone else deleted
it brings it back. `tombstones_overridden_by_local_edit` is reported
rather than hidden, because "my delete came back" needs to be
distinguishable from "the delete never arrived".

### The clock problem

Two devices, two clocks. A tombstone stamped with a naive `now()` loses
to a record the other device touched a minute ago if this device's clock
runs an hour behind - so the user's own deletion bounces back.

Tombstones are therefore stamped `max(now, record.updated + 1s)`,
reading the timestamp from the manifest file being tombstoned. This
makes the local delete authoritative regardless of what the other
clock says.

### Forget, not destroy

`Source.delete()` used to unlink the blob, refcounted against other
sources sharing it. That is now opt-in (`keep_blob=False`); the default
keeps the bytes, because a delete that syncs means the device applying
it may be the last one holding the file. Unlinking would turn someone
else's delete into permanent data loss.

The cost is that `data/blobs` is never reclaimed by this path. Storage
is cheap and portable; a wrongly destroyed lecture PDF is neither.

### Retention: forever, deliberately

Tombstones are a few dozen bytes and they ride in git, so the cost of
keeping them is noise. The reason not to prune is not disk - it is that
a purged tombstone cannot protect against a device that was offline
while it existed. That device re-exports its stale copy, finds nothing
local to compare against, and the record comes back from the dead.
Forever is the only setting that cannot lose a deletion.
`prune_tombstones()` exists but is wired to nothing automatic.

### A gap, stated plainly

Notebooks sync by slug through a single `notebooks/index.json` and
carry no timestamp of their own, so they get no recency comparison and
no tombstone kind is resolved for them yet. Notes, cards and sources -
the things with an `updated` - are covered. A deleted notebook still
resurrects on the next import.

### Verified

`tests/test_sync_tombstones.py` runs against a live SurrealDB with two
namespaces standing in for two devices, because a mocked `repo_query`
agrees with everything interesting. Two failures during development
were both the test's fault rather than the code's, and both are worth
recording because they would have made the suite lie:

  * switching to the "phone" namespace *before* running the PC's export,
    so the export shipped an empty manifest;
  * a fixture that minted a fresh namespace on every call, making
    "switch back to the PC" land in an empty database - the deletion
    then reported `already_absent` and the real behaviour went untested.

A third was real: the assertion compared a tombstone stamped 12:00
against a save made at 10:35 UTC, so the ordering depended on the wall
clock rather than on the rule being tested.

### Closing the notebook gap (which was worse than stated)

Flagging "notebooks carry no timestamp" turned out to be the small part.
The index was written by every export and **read by nothing**. No import
ever created a notebook from it; anything whose slug had no local match
was dropped into `missing_notebooks` and left there. That, not the
missing timestamp, is why seven course notebooks never reached the
phone.

Three changes:

  * `notebooks/index.json` entries carry `updated`, so a notebook is
    subject to the same last-write-wins rule as every other record.
  * import reads the index and creates or renames notebooks by slug,
    before anything that resolves a `notebook_slugs` entry to a local id.
  * `sweep_notebooks` tombstones notebooks that vanished, keyed on a
    synthetic `notebook:<slug>` id - a record id is per-database and
    means nothing on the other device, so the slug is the only identity
    both sides share.

Deleting a notebook on import passes `delete_exclusive_sources=False`.
A source that merely hung off that notebook still exists; it just stops
being filed there. Tombstoning it would delete the user's material
because they tidied a folder.

### An ordering bug worth recording

The notebook sweep was initially placed alongside the per-record sweep,
after the index had been written. It detected nothing, every time.

Per-record files are immune to this: each is its own baseline, and
overwriting one leaves the others as evidence. Notebooks share a single
index, so writing the phone's now-empty list first destroyed the only
record of what the peer believed existed. The sweep has to run *before*
the index is written.

The general lesson is the one from the OCR confidence bug: a detector
that reads its baseline after the baseline has been overwritten returns
a confident zero. Two different modules, same shape of failure.

### A test trap in the new suite

The live tests need a database, so the file originally carried a
module-level `pytestmark` skipif - which skipped the unit tests above it
too. The suite reported green while testing nothing. The marker is now
scoped to the five live tests, so the file reports 18 passed without a
server and 23 with one.

### Deleting a source: forget, and let the setting decide

`Source.delete()` reads `auto_delete_files` rather than hard-coding an
answer, so one switch governs:

    auto_delete_files = "no"   (new default)  record goes, bytes stay
    auto_delete_files = "yes"                 record goes, bytes unlinked

The default flipped from "yes". A delete syncs, so the device applying
it may be the last one holding the file; unlinking there turns someone
else's tidy-up into permanent data loss. The setting still works for
anyone who wants the disk back.

Two deliberate asymmetries:

  * a tombstone application passes `keep_blob=True` explicitly, ignoring
    the local preference. A tombstone is *someone else's* delete
    arriving here, and this device may hold the only copy.
  * reading the setting fails closed toward keeping the file. An
    unreadable preference must not become a destructive one.

The polarity was inverted once on the way in - `keep_blob` was assigned
the helper's return value directly, so "auto delete = yes" ended up
meaning "keep". A test caught it immediately, which is the argument for
having pinned the setting in the test at all rather than inheriting
whatever the developer's database held.

### Two unit tests that had been reading the developer's database

`test_source_delete_without_file` and
`test_source_delete_continues_on_file_error` call `Source.delete()` with
no argument, so they now reach the settings singleton. That record is
real, holds whatever the developer chose, and leaks: with a live
SurrealDB the suite failed on an assertion about
`default_embedding_option`, three fields away from anything under test.

Unit tests must not depend on a developer's saved settings. All three
default-path tests now patch `_auto_delete_files_enabled`.

### Delete confirmation

Audited rather than added to. Confirmation already existed in every
destructive path - the sources list, notebooks, notes, sources column,
transformations, sites, podcasts, credentials each have their own
dialog - and adding more would have been churn, especially with key
parity enforced across fourteen locales.

The one genuine gap: `SourceDetailContent` used native `window.confirm`.
It cannot be styled, has no destructive button, and says nothing about
*which* source is about to disappear - which matters more now, because a
delete propagates. It now uses the app's `ConfirmDialog` with the same
title-based message the list page uses.

Three tests pin the property: the first click does not delete, confirming
does, cancelling does not. Radix's dropdown needs `PointerEvent`, which
jsdom lacks, so the menu is mocked to plain markup like `select` and
`tabs` already are in that file. Its removal left
`sources.deleteSourceConfirm` unused, which a test enforces - dropped
from all fourteen locales rather than left as dead weight.

---

## 16. Retrieval in the chat path

### What was actually happening

`POST /api/chat/context` -> `build_notebook_context` had no notion of the
question. It walked a user-made inclusion list and pasted whole sources
and notes into the prompt. The model received tens of thousands of
characters and was expected to select the relevant part itself.

That is the wrong division of labour. A small model is bad at selecting;
an embedding index is very good at it. On a device built for SDM439,
adding retrieval is the only lever that makes the model question
irrelevant - a bigger model is the lever that makes it unaffordable.

Retrieval already existed and was already tested: `POST /api/search`
with `type: "vector"`, `fn::vector_search` with an HNSW index over 698
`source_embedding` rows. It simply was not wired into chat.

### The bug that hid it

The first version returned nothing, for every query, with no error.

`fn::vector_search` does not return `content`. Its final statement is

    RETURN (select id, parent_id, title, math::max(similarity) as similarity,
            array::flatten(content) as matches
            from $all_results ... group by id, parent_id, title
            ORDER BY similarity DESC LIMIT $match_count);

so a row is one **document** carrying its matching passages in an array
called `matches`, already grouped, with the document's best similarity.
Reading `row["content"]` yields None on every row, every document is
skipped, and the caller reads "no documents" as an empty corpus and
falls back to the whole-document dump.

The failure was indistinguishable from "nothing to retrieve" - which is
the third time in this project that a detector returning a confident
zero was the actual defect. Rendering a real query's output row by row
is what caught it; no unit test had, because the shape was guessed
rather than read.

### The similarity bar

`POST /api/search` defaults to `minimum_score=0.2`. Measured here with
bge-m3: genuine topical matches land at **0.52-0.61**, while "how do I
bake sourdough bread" still returned electrophoresis notes at **0.44**.
At 0.2 that noise is indistinguishable from a real hit, and a small
model will answer from it confidently.

The bar is therefore **0.45**. Above it, a question with nothing to do
with the corpus retrieves nothing, and the caller falls back to whole
documents - which for a general-knowledge question means the model
answers from its priors, exactly as today. No worse, and honest.

### Budget

`limit` bounds *documents*, and each document carries several passages,
so a character ceiling is enforced separately at 6000 (~1500 tokens).
It leaves room in a 4096 window for the system prompt, the question and
the answer. The dump this replaces had no ceiling at all.

### Scope and degradation

The user's inclusion config stays authoritative: retrieval chooses the
content, the checkbox chooses the bookshelf. A passage from an
unselected record is discarded and counted in
`dropped_out_of_scope`.

Every failure degrades rather than raises - no embedding model, index
not built, dimension mismatch. The response carries a `retrieval` report
(`mode`, `documents`, `characters`, `dropped_*`) so "the answer had no
grounding" is visible instead of inferred.

### The blocker this exposed

**No notebook in this database has a single source attached.** All 46
sources sit under no notebook, so notebook-scoped retrieval returns
nothing for them. This is the notebook-grouping item, and it now has a
concrete cost rather than being cosmetic: until sources are grouped,
chat retrieves nothing and always falls back.

### Relinking the recovered sources

The grouping the user described - a source added inside a notebook is
filed there, one added outside is unlinked, and existing ones can be
linked deliberately - **already exists and always worked**
(`POST /notebooks/{id}/sources/{id}`). Nothing needed building.

The problem was narrower. All 46 sources had been recreated by the
recovery scripts, which read decoded records and wrote them straight
into the database with no notebook context, so the manifest held **46
sources, 7 notebooks and 0 reference edges**. And there was no folder
evidence to recover the grouping from: every one of the 45 recovered
files sat in a single flat `/app/data/uploads`, never filed into course
folders in the first place.

`recovery-2026-10-14/relink_sources.py` links only where the source's
own **title** states the subject - substring terms like `genomic
library`, `cdna library`, `crispr`. No content inference, no fuzzy
matching. Anything it is not certain about is left alone and printed,
because a source filed under the wrong course is worse than an
unfiled one: it will be confidently retrieved for the wrong question.

Applied:

    GTB204 - Molecular Biology Techniques   14 sources
    LHP412 - Effective Oral Presentation      2 sources
    deleted                                  9 (8 fixture-throttle, 1 perf-big3)
    left unplaced                           21 (web links, "Eiffel Tower", ...)

37 sources remain, 16 reference edges.

### What that unblocked

Notebook-scoped retrieval resolves through `SELECT VALUE in FROM
reference WHERE out IN $notebook_ids`, so before this every source was
invisible to chat - fully embedded, entirely unfindable. Measured
against GTB204 before and after:

    how is a genomic library constructed
      before: mode=empty, 0 documents
      after:  Genomic library Construction                    0.62

    what does gel electrophoresis separate molecules by
      after:  Electrophoretic Techniques and Molecular Blotting 0.70

### A bug found on the way

The link route's idempotency check read

    SELECT * FROM reference WHERE out = $source_id AND in = $notebook_id

while the `RELATE` directly below it creates `in = source, out =
notebook`. The two are opposites, so the check never matched an existing
edge and every call created another one. SurrealDB accepts a duplicate
`RELATE` silently, so nothing errored - the only symptom was sources
appearing twice in a notebook and retrieval double-counting them.

The orientation is confirmed by the two `artifact` edges that do exist
(`in = note`, `out = notebook`) and is what `export_all` and
`fn::vector_search` both assume. `tests/test_notebook_source_links.py`
pins it; reverting the one-line fix makes it fail.

### The sweep conflated "deleted" with "never received"

Found by calling the phone's own `/sync/export` after it had been
updated, and reading what came back:

    {"counts": {..., "notebooks": 0, "tombstoned": 0}, ...}

`tombstoned: 0` - but only because the phone's manifest had never held
a notebooks index. Had it pulled the PC's manifest first (seven
notebooks) and exported before importing, `sweep_notebooks` would have
compared "seven in the manifest" against "zero held", concluded all
seven were deleted, and written seven tombstones. The PC would then have
applied them - cascading notes with them.

This is not hypothetical. The phone ran a build with no notebook sync
and held zero notebooks while the manifest carried seven, which is
exactly the state that triggers it. The same trap applies to any device
that has not yet completed a first import, and to any record the peer
created after this device's last sync.

**The fix is a sync watermark.** A tombstone is evidence of a deletion
only if the tombstoning device once *had* the record. `import_all`
records when it last completed; the sweep may only tombstone entries
whose `updated` is at or before that mark.

  * never imported -> nothing is deletable
  * entry newer than the last import -> has not arrived yet, not deleted
  * entry with no `updated` -> deletable never; an old manifest cannot
    prove the record arrived, and "cannot prove" must not mean "destroy"

The watermark lives in `DATA_FOLDER`, not in the sync directory. The
sync directory is the shared git repo, and committing a per-device fact
would hand the peer a lie about our history.

`is_deletable` takes the watermark as a parameter with a sentinel, so it
is a pure function. Overloading `None` for both "never imported" and
"go and read it" meant the one case the tests most needed to pin was
decided by whatever a previous run left in `data/`.

### Provenance, verified on the device

After the phone exported, `/api/sources` on it returned:

    21st_century_communication_z_library_sk   origin=file  exported_by=phone  held_by=phone  avail=True
    Electrophoretic Techniques and Molecular   origin=text  exported_by=phone  held_by=None    avail=None

which is the distinction that matters: the record is present either
way, but only one of them can be opened.

---

## 17. onb-update: updating the device

### The problem it solves

Deploying meant remembering five commands in the right order: push a
bundle over adb, copy it into the guest rootfs, merge it, reinstall the
wrappers, restart. Miss the `install-bin.sh` step and the wrappers in
`~/bin` silently stay at the old version - which is exactly how
`onb-embed` drifted away from `termux-embed.sh` in the first place.

    sh ~/bin/onb-update             # update, if the stack is stopped
    sh ~/bin/onb-update --restart   # stop, update, start again
    sh ~/bin/onb-update --check     # report only

### Refusing while the stack is up

Merging underneath a running server leaves a half-old process tree:
Python has already imported the old modules, the worker pool runs old
code, and the next restart behaves differently from the one you just
watched. There is no safe way to do that from outside, so it refuses.

But refusing is a dead end, so `--restart` is the better workaround and
is why the refusal is an error rather than a wall: it stops through the
same path `onb stop` uses, updates, and starts again. One command, and
no window where you have to remember what to run next.

`--check` is exempt. It changes nothing, so refusing would be theatre -
and "is there an update?" is precisely the question worth asking before
deciding to restart anything.

### Nothing to do is success

Already up to date exits 0. Only real failures - a dirty tree, a
non-fast-forward, a missing bundle, the stack being up without
`--restart` - are non-zero. A launcher that treats "no update" as an
error teaches you to ignore the exit code, which costs you the errors
that matter.

### Divergence is refused, not reset

If the guest's HEAD is not an ancestor of the incoming bundle, the
script stops and says so. A silent `reset --hard` would be simpler and
would also throw away any edits made on the device, which is not a
trade worth making on someone's phone unannounced.

### The launch hook

`onb` runs `onb-update --launch-check` before starting anything. It
**checks only** - merging during a launch is the exact conflict the
script refuses, and it would make start-up depend on the network. It is
throttled to once a day (`ONB_UPDATE_MAX_AGE`), because a `git fetch`
on every launch is real latency on a phone for information that rarely
changes. It prints one line when there is something, and is silent on
every other outcome including total failure.

### Three bugs found by testing it off-device

The script is the one piece of this sync story that runs *on the phone*,
against a repo inside proot, fetched from a bundle the operator pushes
over adb - none of which can be exercised from a development box. So
`tests/test_update_script.py` runs the shipping file against a fake
Termux with a real git repo in a fake rootfs, stubbing only
`proot-distro` and `curl`. That immediately paid for itself:

  * **Error messages were being swallowed.** `stage_source` printed its
    failure inside `$( )`, so the most likely failure of all - a bundle
    that was never pushed - exited non-zero with no explanation. Worse,
    `STAGE_ERROR` set inside the substitution never reached the caller,
    so the script reported literally `ERROR: ` followed by nothing.
  * **A bundle was being rev-parsed.** `git rev-parse /path/to.bundle`
    cannot work; a bundle is not a ref. It has to be fetched, exactly as
    the manual procedure does. Every run reported "could not resolve a
    commit" for a perfectly good bundle.
  * **`--check` refused while the stack was up.** Read-only, so refusing
    was theatre, and it was the one case where refusing was least
    useful.

Two further failures were the fixture's fault and are recorded because
each would have made the suite lie: the fake guest was built with
`copytree` (a directory containing the same files, which git correctly
refuses a bundle from), and the "divergent repo" test committed on a
side branch and checked `main` out again - leaving main perfectly
fast-forwardable, so the test passed for entirely the wrong reason.

---

## 18. Chat transcripts as records

### The blocker

A conversation lived only in `data/sqlite-db/checkpoints.sqlite`, read
through `chat_graph.get_state()`. That is a LangGraph-internal
serialisation: opaque, keyed by internal channel names, coupled to the
installed LangGraph version. **There is no union over a checkpoint**, so
a session could not cross devices at all - it had to be retyped. (On the
PC that file was 0 bytes after the recovery; the checkpoint did not
survive it.)

This is not "add another directory to the manifest". The data has to
move somewhere mergeable before it can sync at all.

### Migration 36

    chat_message: id (merge key), session, seq, role, content, created

The checkpoint is **not** removed. It stays the resume cache that makes
a long conversation cheap to continue; it just stops being the only copy.

`id` is generated once, at creation, and carried across devices, so
syncing is an append-only union on record id - the shape the revlog
already uses for review history, and the reason both devices can append
to one conversation with no conflict. `seq` is display order only: two
devices appending independently hand out overlapping values, which is
expected, and ordering falls back to `created` then id.

`GET /chat/sessions/{id}` prefers the table and falls back to the
checkpoint, backfilling as it goes, so a pre-existing conversation
becomes syncable the first time it is opened with no migration step.

### The bug a live database caught

    have = {str(r) for r in existing}     # "chat_message:u1"
    record_id = "u1"                      # bare key
    if record_id in have:                 # never true

The stored id carries the table prefix and the comparison used the bare
key, so **nothing was ever skipped and every turn rewrote the entire
transcript** - a conversation would double each time you sent a message.
Reading it, it looks obviously correct. It was caught by running it
against a real SurrealDB, where `CREATE` failed on the duplicate id.

### Two more, from the same run

**Migration 36 was never going to run.** `AsyncMigrationManager` keeps
hand-maintained up/down lists, and adding the `.surrealql` file does
nothing on its own - the schema it was supposed to create simply would
not exist. `tests/test_migrations_registered.py` now pins that every
file on disk is referenced, that nothing references a missing file, and
that up/down are paired and numbered without gaps.

Same shape as the `~/bin` wrappers drifting from `scripts/termux-*.sh`:
a file that exists in one place and is wired up in another, where the
wire-up is the part that rots.

**The app can update before its migration runs.** The two ship
separately, and the phone pulls code and migrates on next start. Both
read and write paths now tolerate `chat_message` being absent - falling
back to the checkpoint, and never failing a completed turn over
bookkeeping.

### One more from a test mock

The characterisation suite stubs `repo_query` with a single return
value, so the new read call received a `{"out": ...}` row and `row["id"]`
raised, 500-ing the chat page. The mock was unrealistic - but so was the
code, which trusted its rows. Both are fixed: the read filters malformed
rows instead of raising inside a comprehension, and a partial row is a
degraded message rather than a failed request.

---

## 19. Selective history

### The measurement that prompted it

`execute_chat` sent the entire checkpoint history every turn, with no
trimming anywhere. Against the phone's configuration:

    llama-server -c 4096

    system prompt, bare                     618 tokens
    plus 6k of retrieved context         1,404 tokens
    reserving room for the reply           256 tokens
    ----------------------------------------------
    room for history                      2,436 tokens

A real exchange, with citations, is a few hundred tokens rather than my
47-token sample, so call it ten to twenty-five exchanges. That is a
conversation reached over a couple of weeks of study - and reached
*silently*, because the server truncates or refuses rather than saying
"your conversation got too long".

Not an emergency. Still the thing that breaks first on the device class
this fork targets.

### Two ways to be shorter

**By budget** - the default, always applied. Newest exchanges that fit,
never split. A model shown an answer with its question cut off answers
something nobody asked, which is worse than it simply not remembering.
The newest exchange always survives, even alone over budget, because
dropping it leaves the model answering a question it cannot see.

    400 exchanges, 4096 window  ->  kept 32,  3808/3840 tokens
    400 exchanges, 2048 window  ->  kept 15,  1785/1792 tokens

**By timeframe** - `history_since` on the chat request, accepting an ISO
timestamp or shorthand (`45m`, `2h`, `2d`, `1w`). A different tool: not
"as much as fits" but "last night's session". A study conversation spans
days, and Tuesday's exchange may matter more than this morning's four
regardless of budget.

Undated messages survive a timeframe filter. A conversation predating
timestamps in its checkpoint must not be emptied by the control - that
would turn "narrow this" into "delete everything".

### Three bugs, two of them mine

**The timeframe path never applied the budget.** It returned straight
after filtering, so a long afternoon selected with "since this morning"
was sent whole: 26,800 tokens into a 1,792-token budget. The docstring
had promised the budget applied within the window; the code did not. The
budget now runs after the filter, on both paths.

**`select_history` bypassed `parse_since`.** The router parsed the
shorthand before calling, and with only that in place a raw string
reached the function, failed to parse, and was ignored - so the timeframe
control quietly did nothing. It now accepts a datetime or a string
alike. My own test is what found this, by passing `"2h"`.

**A fixture that reversed time.** The test built the conversation
newest-first, so the trimmer appeared to keep the *oldest* exchanges -
which looks exactly like the trimmer working. Real transcripts are
oldest-first; the fix makes the test say so in a comment.

### One number, used twice

`OPEN_NOTEBOOK_CHAT_CONTEXT` is exported by `termux-launch.sh` and used
both as llama-server's `-c` and as the API's trimming budget, from one
literal. Two literals drift, and the symptom is a prompt the server
silently truncates.

### The test suite disarmed the sweep it exists to protect

While writing the selective-history tests I checked whether the PC was
safe to sync, and found `data/sync-watermark.json` containing
`2099-01-01`.

That is the fully-armed state: every manifest entry looks deletable to
this device. Exactly the failure the watermark exists to prevent.

The cause was the test suite. `synced_device`, the autouse fixture that
arms the sweep for tests that are not about the watermark, patches
`read_watermark` to a far-future date. The live round-trip tests under
it call `import_all`, which calls `write_watermark()`, which reads the
**patched** value, sees it is newer than now, and writes it. With
`DATA_FOLDER` unset that landed in the repository's `data/`.

Two files were still leaking after the first fix, so the durable answer
is an autouse fixture in `tests/conftest.py` redirecting `DATA_FOLDER`
for every test in the suite, plus a detector that fails if anything
appears in the project's data directory.

Worth stating plainly: this is the second time in this project that a
guard designed to prevent data loss was itself the cause. The first was
the OCR confidence filter returning a plausible zero. Both were found by
looking at the state rather than by reading the code - the code had been
right in its own terms the whole time.

### What that means for deploying

Before this was caught, the PC would have been the dangerous side rather
than the phone: it is the device holding the manifest copy. A sync run
in that state could have tombstoned records rather than protecting them.
`rm data/sync-watermark.json` disarms the sweep; the first real import
re-arms it correctly, with the actual time.

---

## 20. Chat transcripts over the blob channel

### The rule, and why it is the whole design

The sync repository holds metadata and hashes. A transcript is course
content - the same material as a lecture PDF - so the moment one lands in
git, the repo stops being metadata and becomes a library. Transcripts
therefore travel as blobs over the channel sources already use, and the
manifest records only the hash.

One blob per session, as recommended. The alternative was one per
message: each is tiny, so nothing large is ever in flight. But a
fifty-message conversation becomes fifty blobs and fifty requests, and
the conversation is the unit a person thinks in. A session grows by
rewriting one blob, which content addressing makes cheap. The cost is
size, so there is an advisory at 256 KB - a warning and not an error,
because a conversation long enough to trip it is exactly the one worth
having.

### Merge is an append-only union on record id

Migration 36 already made `chat_message.id` unique and intended it for
this. A message is either present (skip) or new (insert), so both devices
can extend one conversation with no conflict. Ordering is deliberately not
the merge key: two devices assign overlapping `seq` and display order
falls back to `created`, then id.

### Five bugs, four of them mine

**`_export_chats` asked the wrong object.** It called
`ChatMessage(**row).transcript()` on a *chat_session* row, building a
message with no `session` set. The lookup matched nothing, every session
exported with no hash, and the entire blob path did nothing while looking
like it had worked. Now one query fetches all messages, bucketed by
session - which is also fewer round trips than one per session.

**Re-export dropped the hash.** A device that had imported a session but
not its transcript has no messages to serialise, so it wrote
`transcript_sha256: null` - destroying the only record that bytes exist.
The peer would never push them, and the conversation would stay
half-synced for good with no error anywhere. This is the planned
pull-then-export order, not a corner. The previous export is now read
before being overwritten and the hash carried forward, deliberately
*without* claiming to hold the bytes, since that claim is what tells the
peer to push.

**A stale metadata check skipped the transcript.** The bytes arrive on a
later pass than the metadata, so a session whose title has not changed is
exactly the one whose transcript is still missing. The import branched
out before reaching it. Transcript merging is now outside that branch.

**`serialize_transcript` read attributes only.** `parse_transcript`
returns dicts, so re-serialising a parsed transcript turned every message
into an empty one - a conversation reduced to a fraction of its size,
hashed consistently, valid at every step. What surfaced it was the size
advisory *not* appearing, so the assertion that noticed was an unrelated
looking one. `_field` now reads either form.

**Hashes depended on arrival order.** Sorted JSON keys fixed key order
but not list order, so two devices holding the same messages in different
arrival order produced different blobs and each pushed its own copy
forever. Messages are now sorted by (seq, id) before serialising - the
same ordering the merge uses, so nothing downstream depends on it.

### The tombstone path had a live landmine

Unrelated to chats, found while adding sessions to it. `_apply_tombstones`
called `existing.delete(keep_blob=True)` unconditionally. Only `Source`
accepts that argument; `Note`, `Flashcard` and `ChatSession` inherit
`ObjectModel.delete(self)`. So a tombstone for a record this device
actually held raised `TypeError`, part-way through applying the rest.

**Delete a note on the phone and the PC's import dies.** No test caught
it: `keep_blob` appeared nowhere in the tombstone tests. The flag is now
passed only to a `delete` that asks for it, checked by signature rather
than caught - a `try` would swallow a `TypeError` thrown from inside a
delete for an unrelated reason and leave the record half-removed.

Chat sessions also clear their messages when tombstoned.
`chat_message.session` has no `ON DELETE CASCADE`, so deleting a session
leaves rows behind - invisible, since every read is scoped by session,
but adopted by any session later re-imported under the same id. A
conversation the user deleted comes back with the parts they removed.

### Fourth time a guard was the cause

The OCR confidence filter returned a plausible zero. The test suite wrote
a 2099 watermark into the real data directory. `pid_of` reported a 3 MB
process as a running server. `serialize_transcript` discarded a whole
conversation. Every one was a safety mechanism, and in three of the four
the failure looked like the mechanism working - a filter returning
nothing, a sweep tombstoning nothing, a status line reporting a dead
server as up.

None were found by reading the code, which was correct in its own terms
throughout. Each was found by looking at the state and asking whether the
number was plausible.

---

## 21. The first sync, and the failures it took to get there

### The container was serving a build without sync in it

`sh scripts/pc-sync.sh push` returned `{"detail":"Not Found"}` from
`/api/sync/export`. The router existed in the source, tested and green -
but the PC app runs in a Docker container built from an image, and the
image predated it. Only `notebook_data` was mounted, so even a working
export would have written the manifest into the container where no
host-side git could see it, and `sync/` was not gitignored, so it would
also have tried to become a gitlink - putting transcripts in the app
repo's history, the one thing this design exists to prevent.

All three were true at once. The export would have looked like it
worked, committed nothing, and written traces into the app repository
for good measure.

### The blob store the app reads was empty

Fifteen hashes recorded, fifteen files on disk, `resolve_blob_path`
finding none of them. The sources had been migrated to content
addressing correctly - `file_path` was already
`data/blobs/<aa>/<sha256>.<ext>` - but there are two stores:

    ./data/blobs             21 files   the real store
    notebook_data/blobs       0 files   what the container mounts

`BLOBS_FOLDER` is relative to whichever process asks, so the host means
one directory and the container another. Both are correct; they are just
different. Copied and verified by re-hashing, never moved or deleted.

The repair script's first version asked the app where its blobs were,
which on the host is the store they were already in, so it reported
"nothing to do" while the app could open none of them: a comparison
between a thing and itself. Both roots are named explicitly now.

And the six chat sessions were read as test residue and very nearly
deleted. They were real conversations from 12-22 September. The
placeholder rows belonged to `chat_session:abc` and `chat_session:n`,
which do not exist - the real sessions simply have no `chat_message`
rows, because their transcripts live only in the LangGraph checkpoint.
A zero-sample "no transcript text in git" check would then have passed
for the wrong reason: there were no transcripts to leak.

### A failed pull reported itself as a conflict

The phone's pull was not a divergence at all - `git pull --ff-only` on
an unborn branch succeeds (verified). The remote needed credentials the
fresh Termux git had never been given, and the driver discarded stderr
and called every failure "someone edited the same record". The operator
then tried to resolve a conflict on a branch never committed to.

The pull now captures stderr and classifies it. A network error or a
refused credential says so and points at `ls-remote`; only a real
non-fast-forward is called a conflict. Same change on the PC driver.

Also reordered while there: `pull` checked the app *before* fetching,
so a stopped app hid a broken remote behind a message about starting
things. Git first, then the import gate - the git half needs no app.

### The pattern by now

Six guards in this project have failed in the direction of their own
plausibility: a filter returning nothing, a sweep tombstoning nothing, a
status line reporting a dead server as up, a serialiser discarding a
conversation, a script reporting nothing to do, and a pull diagnosing a
conflict that did not exist. None were found by reading the code. Each
was found by asking whether the number on screen was true.

---

## 22. The import filed nothing, twice over

The phone pulled 7 notebooks and 46 sources, the import reported
success, and every notebook opened empty.

Two defects combined, in the same function. First, the notebook map was
snapshotted before notebooks were imported, so on a device receiving new
notebooks every slug missed: the notebooks were created and then nothing
was ever filed into them. The comments even said the ordering mattered -
"this has to run before the notes and sources are processed" - about the
import, while the map it resolves against stayed stale above it.

Second, sources were filed as `artifact` edges. The export tolerates both
tables, but the notebook view reads `reference` - the table the UI
writes. So even a fresh map would have filed every source where
`get_sources` never looks: attached by every measure except the one the
user sees. Sessions were worse: the manifest carried `notebook_slugs`
for them from the start and the import ignored the field entirely, so
synced conversations arrived unfiled anywhere.

The repair is threefold: the map is rebuilt after notebooks import (and
tombstone application uses the fresh one too); one `_attach_slugs`
helper files notes with `artifact`, sources with `reference`, sessions
with `refers_to` - the tables each reader actually reads; and filing runs
for stale records as well as fresh ones, idempotently, so a repeated
import repairs a missed attachment instead of skipping past it again.
RELATE always inserts, so the helper skips edges already present - a
second import must not double every membership.

One existing test asserted the bug: it expected a slug in
`missing_notebooks` that only missed because the map was stale. It now
asserts the source is filed and nothing is missing.

---

## 23. The bytes arrived and nothing could open

The phone reported `sources_awaiting_blobs: 0` - every hash resolved -
while every file opened as "No content available". Both true: the detail
view reads `source.full_text`, and it was never written. Import creates
the record with no text (there is no file yet), the blob channel delivers
the file, and no hook connected the two. The design said "re-extracted
once it arrives"; nothing did it.

`enqueue_missing_extraction` is the hook: sources with a resolvable file
but no text and no command get the same `process_source` job an upload
submits. Import calls it on every real pass; the blob pull endpoint
calls it after fetching; a manual `/sync/blobs/process` endpoint covers
the rest. Only never-processed sources qualify - a job that already ran,
even failed, is left for explicit retry rather than a silent requeue
that risks duplicating embeddings. Transformations are not re-run (empty
list): readable text plus embeddings now, the owner's choices later.

The call is guarded, not merely made: queuing is an enhancement to the
import, andmust never break the thing it follows. The mocked import
tests proved the point immediately - the scan hit a real database
through their mocks and six of them failed with 403s before the guard
existed.

---

## 24. The probe said readable; the extractor found nothing

The phone and the PC both failed the same PDF with "Could not extract
any text content", and the log held zero OCR lines - `_try_ocr` never
ran on either. It only runs when no text layer is detected, and
`_pdf_text_layer_present` returns True on any uncertainty. The file has
a layer the extractor cannot read; the probe called it readable, the
engine found nothing, and the heuristic's optimism foreclosed the one
path that works. My own OCR module reads 10,418 chars from the file
when called directly.

The extractor's emptiness now decides, not the probe: when a PDF
extracts to nothing and OCR is available, the pipeline OCRs as a last
resort before raising. Verified against the real file through the real
path, not mocks.

The failure message named the wrong remedy for every device in this
project: "turn on OCR by enabling the Docling document engine" - on a
phone where Docling is deliberately absent and on a container that never
installed tesseract. It now reports what was tried: attempted-and-empty
names the document, unavailable names the missing binary the package
manager understands.

Two environment gaps closed alongside. The PC container never had
tesseract (`MISSING`), so no scanned PDF could ever process there; the
Dockerfile installs it with eng+jpn data to match the default langs.
And the manifest gitignore (`sync/`) matched the `open_notebook/sync/`
package and silently kept the extraction module out of git - local tests
passed, the bundle shipped without it, the phone 500'd while reporting
itself up to date. The rule is root-anchored and a test asserts every
source file is tracked.
