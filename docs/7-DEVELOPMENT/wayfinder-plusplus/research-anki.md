# Anki integration research (read-only)

Date: 2026-10-11. Sources are PRIMARY unless marked `(secondary)`.
Canonical AnkiConnect source is now sourcehut — the GitHub repo is archived
read-only with a pointer to `https://git.sr.ht/~foosoft/anki-connect`.
Anki manual pages via `docs.ankiweb.net`. FSRS via the Anki manual +
`open-spaced-repetition` org repos. No Anki integration exists in this repo
today (grep for `anki|apkg|fsrs|spaced repetition` hits only unrelated
"ranking/reranking" strings), so this is greenfield.

## 1. AnkiConnect API surface (primary: https://git.sr.ht/~foosoft/anki-connect)

### Transport / auth / envelope

- AnkiConnect is an Anki **desktop add-on** (install code `2055492159`,
  Tools → Add-ons → Get Add-ons, restart). It starts an **HTTP server on
  port 8765 whenever Anki is launched**. Verify at `localhost:8765`
  (browser shows "Anki-Connect"). All per
  https://git.sr.ht/~foosoft/anki-connect (Installation + Developer sections).
- Every request is a JSON object `{action, version, params, key?}`; every
  response is `{result, error}` with `error: null` on success, e.g.
  `{"result": ["Default", "Filtered Deck 1"], "error": null}`. Omitting
  `version` defaults to 4 (legacy result-only shape). Current version is **6**.
  Sample clients given for curl / PowerShell / Python (`urllib`) / JS (`XHR`).
- **Bind address:** `127.0.0.1` by default (same-host only). LAN access via
  add-on config `webBindAddress` (e.g. `0.0.0.0`) + restart.
- **Auth:** off by default; set `apiKey` in add-on config, then every request
  carries `key`. `requestPermission` is the handshake: untrusted origins
  (anything not in `webCorsOriginList`; `localhost` trusted by default) pop a
  permission dialog in Anki, denials can be persisted to `ignoreOriginList`.
- `multi` batches sub-actions in one POST. `apiReflect` with
  `scopes: ["actions"]` lists supported actions (useful for version probing).

### Does it require the desktop app running on localhost:8765? Yes.

- "Anki **must be kept running in the background** in order for other
  applications to be able to use Anki-Connect" (sourcehut page, verbatim).
- Headless/offline consequences:
  - **No Anki process → connection refused.** There is no standalone
    AnkiConnect daemon; it lives inside the Qt app event loop.
  - Windows: firewall prompt on startup (must unblock Anki). macOS: App Nap
    suspends background Anki and kills the server (documented `defaults write
    ... NSAppSleepDisabled` workaround). Both per sourcehut Notes sections —
    relevant if open-notebook runs alongside Anki on user machines.
  - `gui*` actions additionally need the GUI state they drive (open Browser,
    reviewer on a card, Add dialog open for `guiAddNoteSetData` which errors
    otherwise). Prefer the non-GUI equivalents (`findCards` over `guiBrowse` —
    documented as "functionally identical... but doesn't use the GUI for better
    performance").
  - Server-side / container deployment (open-notebook's docker-compose
    includes its own stack, no Anki) **cannot** rely on AnkiConnect. That is
    the case for the `.apkg` fallback (§4).

### Action inventory (exact names, v6 request shapes)

**Note actions** (the core push path):

| Action | Params shape | Notes |
|---|---|---|
| `addNote` | `note: {deckName, modelName, fields: {F: v}, tags?, options?, audio?, video?, picture?}` → returns note id (`1496198395707`), `null` on failure | `options.allowDuplicate` (default false → duplicate throws), `options.duplicateScope` (`"deck"` vs whole collection), `duplicateScopeOptions: {deckName, checkChildren (def false), checkAllModels (def false)}`. Media entries: `{filename + one of data (base64) / path / url, skipHash?, fields?}` — precedence data > path > url. `fields` lists which note fields to append the media reference to |
| `addNotes` | `notes: [...]` → array of ids; errors aggregated into `error` string | Batch create |
| `canAddNotes` / `canAddNotesWithErrorDetail` | `notes: [...]` → `[bool]` / `[{canAdd, error?}]` | Pre-flight duplicate check; use before `addNotes` |
| `updateNoteFields` | `note: {id, fields, audio?, video?, picture?}` | WARNING (documented): note must **not** be open in the Browser or fields silently don't update |
| `updateNote` | `note: {id, fields?, tags?, audio?, video?, picture?}` = updateNoteFields + updateNoteTags; fields-first, no rollback | At least one of `fields`/`tags` required |
| `updateNoteModel` | `note: {id, modelName, fields, tags}` | Model switch |
| `updateNoteTags` / `getNoteTags` | `note: <id>` + `tags` | Replace-all semantics |
| `addTags` / `removeTags` | `notes: [ids], tags: "<space-separated>"` | Note: `tags` is a **string** here, not array |
| `getTags` / `clearUnusedTags` | — | Collection tag list |
| `replaceTags` / `replaceTagsInAllNotes` | `tag_to_replace`, `replace_with_tag` (+ `notes` for scoped) | |
| `findNotes` | `query` (Anki search syntax, https://docs.ankiweb.net/searching.html) → `[noteId]` | |
| `notesInfo` | `notes: [ids]` **or** `query` → `[{noteId, profile, modelName, tags, fields: {F: {value, order}}, mod, cards: [cardIds]}]` | Main read path for sync-back |
| `notesModTime` | `notes: [ids]` → `[{noteId, mod}]` | Cheap change detection |
| `deleteNotes` | `notes: [ids]` (deletes child cards too) | No soft-delete; exports can't propagate deletes either |
| `removeEmptyNotes` | — | |

**Card actions:** `getEaseFactors`, `setEaseFactors` (`cards`, `easeFactors`),
`setSpecificValueOfCard` (`card`, `keys`, `newValues`, some keys need
`warning_check`), `suspend`/`unsuspend` (`cards`), `suspended` (`card`),
`areSuspended`, `areDue`, `getIntervals` (`cards`, `complete?`; negatives =
seconds, positives = days), `findCards` (`query`), `cardsToNotes`,
`cardsModTime`, `cardsInfo` (question/answer incl. CSS, deckName, modelName,
interval, due, reps, lapses, left, mod), `forgetCards`, `relearnCards`,
`answerCards` (`answers: [{cardId, ease 1–4}]`, timer starts on call),
`setDueDate` (`cards`, `days`: `"0"` = today, `"1!"`, `"3-7"` ranges).

**Deck (collection) actions:** `deckNames`, `deckNamesAndIds`, `getDecks`
(cards → deck map), `createDeck` (`deck`, `::` nesting, idempotent, returns
deck id), `changeDeck` (`cards`, `deck`), `deleteDecks` (`decks`, `cardsToo:
true` **required**), `getDeckConfig`/`saveDeckConfig` (legacy
new/lapse/rev config group object — pre-FSRS shape), `setDeckConfigId`,
`cloneDeckConfigId`, `removeDeckConfigId`, `getDeckStats`
(`decks` → `{new_count, learn_count, review_count, total_in_deck}`).

**Media actions:** `storeMediaFile` (`filename` + `data`/`path`/`url`,
`deleteExisting` default true overwrites; **prefix config-style files with
`_`** so Anki doesn't garbage-collect them — still synced), `retrieveMediaFile`
→ base64 or `false`, `getMediaFilesNames` (`pattern`), `getMediaDirPath`,
`deleteMediaFile`. `addNote`'s audio/video/picture delegate to the same
uploader.

**Misc actions:** `requestPermission`, `version` (returns `6`), `apiReflect`,
`sync` (**no params — triggers AnkiWeb sync of the local collection**),
`getProfiles`, `getActiveProfile`, `loadProfile` (`name`), `multi`,
`exportPackage` (`deck`, `path`, `includeSched` default false → `true`/`false`),
`importPackage` (`path` — **relative to `collection.media`**, sic),
`reloadCollection`.

**Model (note-type) actions:** `modelNames`, `modelNamesAndIds`,
`findModelsById`, `findModelsByName` (full schema incl. `flds`/`tmpls` with
stable `id`s, `css`, `req`), `modelFieldNames`, `modelFieldDescriptions`,
`modelFieldFonts`, `modelFieldsOnTemplates`, `createModel`
(`modelName`, `inOrderFields`, `cardTemplates: [{Name?, Front, Back}]`,
`css?`, `isCloze?`), `modelTemplates`, `modelStyling`,
`updateModelTemplates`, `updateModelStyling`, `findAndReplaceInModels`,
`modelTemplateRename/Reposition/Add/Remove`, `modelFieldRename/Reposition/Add/
Remove/SetFont/SetFontSize/SetDescription`.

**Statistics actions** (review-history extraction for FSRS/optimizer input):
`getNumCardsReviewedToday`, `getNumCardsReviewedByDay`,
`getCollectionStatsHTML` (`wholeCollection`), `cardReviews`
(`deck`, `startID` → list of 9-tuples
`(reviewTime, cardID, usn, buttonPressed, newInterval, previousInterval,
newFactor, reviewDuration, reviewType)`),
`getReviewsOfCards` (`cards` → per-card dicts with Anki DB keys
`id/usn/ease/ivl/lastIvl/factor/time/type`),
`getLatestReviewID` (`deck`), `insertReviews` (write 9-tuples back).

## 2. Push (AnkiConnect) vs sync (.apkg/AnkiWeb) tradeoffs

| Dimension | AnkiConnect push | `.apkg` file exchange | AnkiWeb sync (`sync` action) |
|---|---|---|---|
| Requires | Desktop Anki + add-on + same host (or LAN bind) | Nothing but a file; works headless/offline, generatable server-side | AnkiWeb account + network + desktop Anki to run the sync |
| Direction | App → collection (writes), plus reads (`notesInfo`, `cardsInfo`, `cardReviews`) for sync-back | App → file → user imports (manual step in Anki: File → Import) | Collection ⇄ AnkiWeb ⇄ all devices; merges reviews/edits from both ends |
| Scheduling data | Untouched: new notes enter as new cards under the deck's preset (FSRS applies on review) | Excluded by default (`Include Scheduling Information: false` → clean deck for sharing); opt-in via `includeSched`/`exportPackage` | Preserved; both reviews kept if a card was answered on two devices, newest answer wins |
| Updates | `updateNote*` by id — but fails silently if note open in Browser; duplicates controlled per-call | Re-import updates notes whose file copy is **newer** (mod-time compare); **blocked if note type changed**; Anki 23.10+ adds force/never/merge modes | Normal edits merge; structural changes (new field, dropped template) force a **one-way** full sync choice |
| Deletes | `deleteNotes` immediate | **Not propagated** (import only adds/updates) | Propagated (with media caveats) |
| Media | `storeMediaFile` + field refs, immediate | Bundled (`Include Media`); user imports one file | Synced separately, may lag cards; edits to existing files don't sync unless file added/removed |
| Failure modes | Connection refused (Anki closed), permission dialog (untrusted origin), silent no-op on open-in-Browser | Stale re-imports ignored; scheduling accidentally included → "unexpectedly large intervals" (fix: Set Due Date / uncheck "Import any learning progress") | Conflict dialogs; forced one-way sync on structural change; 6-month AnkiWeb expiry |

Sources: AnkiConnect action semantics from https://git.sr.ht/~foosoft/anki-connect;
`.apkg` behavior from https://docs.ankiweb.net/exporting.html and
https://docs.ankiweb.net/importing/packaged-decks.html; sync semantics from
https://docs.ankiweb.net/syncing.html.

Recommendation for open-notebook: **both, layered** — AnkiConnect as the
live path (instant push + `notesModTime`/`notesInfo` sync-back when the user
has desktop Anki), `.apkg` download as the universal fallback (works from the
container, offline, mobile). Never present `sync` as headless sync: it only
works when desktop Anki with credentials is running.

## 3. Media / attachment path

- Anki notes reference media by **bare filename in field HTML**, not paths:
  `<img src="myimage.jpg">`, `[sound:myaudio.mp3]`
  (https://docs.ankiweb.net/importing/text-files.html, "Importing Media").
  Template-level `<img src="{{field}}">` is **not supported**.
- Files live flat in `collection.media` — **no subdirectories**
  ("Do not put subdirectories in the media folder, or some features will not
  work"); collection layout (`collection.anki2` + `collection.media/`) per
  https://docs.ankiweb.net/files.html.
- Via AnkiConnect: `storeMediaFile` first (base64 `data` preferred for
  server-generated bytes; `path` for local files; `url` lets Anki download),
  then include the filename in the field value or pass
  `audio/video/picture: [{filename, fields: [...] }]` on `addNote`/`guiAddCards`
  (sourcehut `addNote`/`storeMediaFile` docs). `skipHash` guards against
  persisting error-page downloads.
- Via `.apkg`: tick `Include Media`; export bundles local media, import drops
  it into the user's `collection.media`. Filenames should be content-hashed by
  the exporter to avoid collisions across decks.
- Cleanup hazard: Anki's Check Media deletes files unreferenced by any card;
  prefix helper/config files with `_` (documented in `storeMediaFile`).

## 4. Offline `.apkg` fallback design (from docs behavior)

Format facts (https://docs.ankiweb.net/exporting.html):

- `.apkg` = "deck package": **one deck (+ children)** with its cards, notes,
  **note types**, and sounds/images. (Filename `collection.apkg` is reserved —
  treated as a full collection package, so never emit that name.)
- Export options that map 1:1 to our exporter settings: **Include Scheduling
  Information** (default off for sharing — stripped incl. marked/leech tags),
  **Include Deck Presets** (carries FSRS parameters + desired retention —
  want ON so FSRS follows the deck), **Include Media**, **Support older Anki
  versions** (legacy = slower/larger; modern = faster + compressed media).
- Import is **additive with newest-wins update**: previously-imported notes are
  kept at whichever side has the newer mtime
  (https://docs.ankiweb.net/importing/packaged-decks.html, "Updating").
  Updating breaks if the note type changed; Anki 23.10+ offers force/never/
  **merge** (merge needs field/template ids from Anki ≥ 2.1.67 and forces a
  full sync — design note types once, with stable ids).
- Plain-text (TSV) import is the lighter alternative: first field (or GUID
  column) is the identity key, `Allow HTML` preserves formatting, headers can
  pin `notetype`/`deck`/columns
  (https://docs.ankiweb.net/importing/text-files.html). But media must be
  pre-placed in `collection.media`, so TSV is strictly worse than `.apkg` for
  a download flow — prefer `.apkg`.

Proposed exporter shape for open-notebook (behavioral contract; confirm byte
layout against `ankitects/anki` exporter source at implementation time):

1. One deck per notebook/export, e.g. `OpenNotebook::<title>`; one stable
   note type (e.g. `OpenNotebook Basic` + optional Cloze), fields fixed at v1.
2. Stable note GUIDs derived from the source object id → re-downloads update
   instead of duplicating (newest-wins rule).
3. `includeSched: false`, `include presets: true`, `include media: true`,
   modern (non-legacy) package.
4. Media filenames `nb_<sha1>.<ext>`, referenced inline in field HTML.
5. Serve as a download (`Export .apkg` button / API endpoint); document the
   two-click import (File → Import, or double-click) and the "uncheck Import
   any learning progress" remedy for polluted scheduling.
6. (Secondary, non-primary tooling) the community `genanki` Python library is
   the established `.apkg` writer if we don't want to own the SQLite/zip
   layout; evaluate at implementation time.

## 5. FSRS relevance

Authoritative behavior (https://docs.ankiweb.net/deck-options.html, FSRS
section; background https://github.com/open-spaced-repetition/fsrs4anki):

- FSRS (Free Spaced Repetition Scheduler) is **built into Anki ≥ 23.10**,
  replacing legacy SM-2; older clients (incl. AnkiDroid < 2.17) break
  scheduling, so exports should note the minimum version.
- It is a **global toggle**, but **parameters + desired retention are
  per-preset** — our exported deck should carry its own preset (see §4.3).
- **Desired retention (default 90%) is the dominant control**: higher retention
  → shorter intervals → more reviews/day; >97% workloads "can be overwhelming".
  Do not set retention for the user; ship the 90% default.
- **Parameters are ML-fitted, never hand-tuned**: the Optimize button fits
  weights to the deck's review history (needs a few hundred reviews; re-fit
  ~monthly; separate presets for wildly different difficulties). Documented
  failure modes: review count too low, **"Hard" misuse** (Hard = recalled with
  effort, never "forgot" — misuse inflates all intervals), not pressing Again
  on forgetting.
- Keep learning/relearning steps **under 1 day** (e.g. `10m`, `30m`); FSRS-5+
  can own sub-day scheduling with steps left empty (experimental). Enabling
  FSRS / changing retention / changing params optionally **reschedules all
  cards** — default off, keep off on first export.
- **Add-on compatibility rule: anything that touches intervals is incompatible
  with FSRS** (named: Delay siblings, Auto Ease Factor, autoLapseNewInterval,
  Straight Reward → use FSRS Helper instead). Implication: our integration must
  not write intervals/ease directly (`setEaseFactors`, `setDueDate`,
  `setSpecificValueOfCard`, `insertReviews`) except in explicitly expert flows.
- Custom scheduling JS still exists as a global escape hatch, but built-in
  FSRS supersedes the `fsrs4anki_scheduler.js` shim — do not ship scheduler JS.

What this means for open-notebook:

1. **Don't build a scheduler for the Anki path.** Push notes (or `.apkg`
   without scheduling) and let the user's Anki + FSRS preset do the scheduling.
   FSRS parameters travel with the deck preset on export.
2. **If open-notebook wants native (in-app) spaced repetition**, use an
   official FSRS port, not a reimplementation: `py-fsrs` (`pip install fsrs`)
   exposes `Scheduler(parameters=(21 weights…), desired_retention=0.9,
   learning_steps, relearning_steps, maximum_interval=36500,
   enable_fuzzing)` with `Card`/`ReviewLog` JSON-serializable for DB storage,
   plus an optional `Optimizer` (`pip install "fsrs[optimizer]"`) for
   `compute_optimal_parameters` / `compute_optimal_retention`
   (https://github.com/open-spaced-repetition/py-fsrs). Sibling ports
   (`ts-fsrs` for the frontend, `fsrs-rs`, etc.) are indexed at
   https://open-spaced-repetition.github.io/awesome-fsrs/. Note the weight-count
   is versioned (17 in FSRS-4.5, 19 in FSRS-5, 21 in FSRS-6 — per the
   per-version notes on that index), so pin and record the FSRS version
   alongside any stored parameters.
3. Native review history (`cardReviews`/`getReviewsOfCards` 9-tuples) can seed
   an optimizer, but that requires the desktop-Anki path; the `.apkg` path
   yields no history.

## Source list (fetch these first for implementation)

- https://git.sr.ht/~foosoft/anki-connect — AnkiConnect API reference (actions,
  envelopes, auth, media, sync). GitHub copy archived.
- https://docs.ankiweb.net/exporting.html — `.apkg`/`.colpkg` semantics +
  export options.
- https://docs.ankiweb.net/importing/packaged-decks.html — import merge/update
  rules, scheduling-on-import, 23.10 merge.
- https://docs.ankiweb.net/importing/text-files.html — TSV alternative, media
  refs, GUID/first-field identity, file headers.
- https://docs.ankiweb.net/deck-options.html — FSRS section (retention,
  parameters, optimizer, presets, steps, simulator).
- https://docs.ankiweb.net/syncing.html — AnkiWeb merge/conflict/media rules.
- https://docs.ankiweb.net/files.html — `collection.anki2` + `collection.media`
  layout, flat-media rule.
- https://github.com/open-spaced-repetition/fsrs4anki — scheduler/optimizer
  background + add-on compatibility table.
- https://github.com/open-spaced-repetition/py-fsrs — `Scheduler`/`Optimizer`
  API for native scheduling.
- https://open-spaced-repetition.github.io/awesome-fsrs/ — FSRS ports per
  language + FSRS version notes.
