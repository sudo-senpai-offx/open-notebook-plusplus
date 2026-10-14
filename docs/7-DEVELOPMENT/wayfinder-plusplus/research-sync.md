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
