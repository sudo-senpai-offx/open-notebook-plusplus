# SRS Research — FSRS vs SM-2, Card Design, Metrics

Status: research-only, no code changed.
Date: 2026-10-11.
Audience: Wayfinder++ track in `open-notebook` (Python FastAPI backend + SurrealDB; see `pyproject.toml` requiring `python>=3.11,<3.13`, FastAPI, SurrealDB, LangChain/LangGraph stack).
Target path: `docs/7-DEVELOPMENT/wayfinder-plusplus/research-srs.md` (this file).

All claims below cite primary sources (library repos, Anki manual, SuperMemo author pages). Where a claim is analysis/synthesis rather than a vendor statement, it is labeled as such.

---

## 1. FSRS algorithm (what it is)

- FSRS (Free Spaced Repetition Scheduler) models memory with the DSR model: Retrievability `R` (probability of recall), Stability `S` (interval at which R=90%), Difficulty `D` in [1,10], and Grade `G` 1=Again, 2=Hard, 3=Good, 4=Easy ([awesome-fsrs wiki: The Algorithm](https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm)).
- Current stable lineage in the libraries is **FSRS-6 with 21 parameters**; default weights `[0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542]` ([awesome-fsrs wiki: The Algorithm, FSRS-6](https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm), [py-fsrs README custom-parameters](https://github.com/open-spaced-repetition/py-fsrs), [ts-fsrs badge FSRS-v6](https://github.com/open-spaced-repetition/ts-fsrs)).
- FSRS-6 forgetting curve is trainable-decay: `R(t,S) = (1 + factor·t/S)^(-w20)` with `factor = 0.9^(-1/w20) − 1` so that `R(S,S)=90%`; same-day stability update is `S′(S,G) = S·e^(w17·(G−3+w18))·S^(−w19)` ([awesome-fsrs wiki: The Algorithm, FSRS-6 formula](https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm)).
- Card states are Learning / Review / Relearning, ratings Again/Hard/Good/Easy ([py-fsrs README Reference](https://github.com/open-spaced-repetition/py-fsrs), [ts-fsrs docs Reference](https://open-spaced-repetition.github.io/ts-fsrs/)).
- Origin: FSRS derives from the DHP/DSR model in MaiMemo's ACM KDD / IEEE TKDE papers ([awesome-fsrs wiki citing MaiMemo papers](https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm), [fsrs4anki wiki pointer](https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm)).
- Benchmark context: the project maintains an open benchmark (`srs-benchmark`) on ~10k Anki users / ~727M reviews (hosted as `open-spaced-repetition/anki-revlogs-10k` on Hugging Face), evaluated with time-series split on Log Loss, RMSE(bins), AUC; FSRS versions sit well above SM-2-era baselines (HLR, AVG) and below large neural models (GRU/LSTM/RWKV) which cost 10^2–10^6 parameters vs FSRS-6's 21 ([srs-benchmark README: Dataset, Evaluation, Result tables](https://github.com/open-spaced-repetition/srs-benchmark)).

## 2. Library recommendations (weight / license / API)

### 2.1 Recommendation for open-notebook

- **Primary: `py-fsrs` (`pip install fsrs`, package name `fsrs`, v6.3.2 at time of research) in the Python backend.** Rationale: open-notebook is a Python/FastAPI service ([pyproject.toml](https://github.com/open-spaced-repetition/py-fsrs/blob/main/pyproject.toml) context: repo `pyproject.toml` pins `requires-python >=3.11,<3.13` with FastAPI/SurrealDB), so scheduling should live beside the existing Python domain code, reuse `Card`/`ReviewLog` JSON serialization for SurrealDB storage, and avoid adding a Node runtime to the review path ([py-fsrs README Quickstart/JSON-Serialization](https://github.com/open-spaced-repetition/py-fsrs)).
- **Fallback: hand-rolled SM-2 (~30 lines, zero dependency) or `sm-2` package.** Use when review history is thin (< few hundred reviews — Anki/FSRS docs note the optimizer needs data; FSRS deck-options advises optimizing only after history accumulates and re-optimizing ~monthly) or when determinism/offline simplicity matters ([Anki Deck Options: FSRS Parameters/Optimize](https://docs.ankiweb.net/deck-options.html), [py-fsrs README Optimizer](https://github.com/open-spaced-repetition/py-fsrs)).
- **Do not adopt `ts-fsrs` scheduler for v1** unless a client-side (frontend) preview-before-answer flow is required; it duplicates the Python scheduler. If that flow is later needed, `ts-fsrs` `repeat()` previews all four ratings before the answer is known, while `next()` applies the chosen rating ([ts-fsrs docs: repeat vs next](https://open-spaced-repetition.github.io/ts-fsrs/)).
- **Defer the optimizers (both ecosystems) to an offline/batch job.** Both pull heavy ML stacks (see weight below); ship defaults first, optimize per-deck/preset later ([Anki Deck Options: optimize per preset, monthly cadence](https://docs.ankiweb.net/deck-options.html), [py-fsrs README Optimizer](https://github.com/open-spaced-repetition/py-fsrs)).

### 2.2 `py-fsrs` — API shape

- Install: `pip install fsrs` ([py-fsrs README Installation](https://github.com/open-spaced-repetition/py-fsrs)).
- Core objects: `Scheduler, Card, Rating, ReviewLog` ([py-fsrs README Quickstart](https://github.com/open-spaced-repetition/py-fsrs)).
- Minimal flow ([py-fsrs README Quickstart](https://github.com/open-spaced-repetition/py-fsrs)):
  ```python
  from fsrs import Scheduler, Card, Rating
  scheduler = Scheduler()  # defaults: 21 FSRS-6 weights, desired_retention=0.9
  card = Card()            # new cards are due immediately
  card, review_log = scheduler.review_card(card, Rating.Good)
  ```
- Ratings: `Rating.Again==1` forgot; `Hard==2` serious difficulty; `Good==3` hesitation; `Easy==4` easily recalled ([py-fsrs README Quickstart](https://github.com/open-spaced-repetition/py-fsrs)).
- Tunables ([py-fsrs README Custom parameters](https://github.com/open-spaced-repetition/py-fsrs)): `parameters` (21 weights, leave at default unless optimizing), `desired_retention` 0–1 (default 0.9; higher = more reviews), `learning_steps` (default 1m,10m; `()` disables), `relearning_steps` (default 10m; `()` keeps lapsed cards in Review), `maximum_interval` (default 36500 days), `enable_fuzzing` (default True; small random jitter so same-day siblings don't clump).
- Introspection: `scheduler.get_card_retrievability(card)` returns current P(recall) ([py-fsrs README Retrievability](https://github.com/open-spaced-repetition/py-fsrs)).
- Persistence: `Scheduler`/`Card`/`ReviewLog` each have `to_json()`/`from_json()` for DB/network storage ([py-fsrs README JSON-Serialization](https://github.com/open-spaced-repetition/py-fsrs)).
- Timezone: UTC only; custom datetimes must carry UTC tzinfo ([py-fsrs README Timezone](https://github.com/open-spaced-repetition/py-fsrs)).
- Optimizer (optional, extra install `pip install "fsrs[optimizer]"`): `Optimizer(review_logs).compute_optimal_parameters()` then `compute_optimal_retention(params)`; `scheduler.reschedule_card(card, logs_for_card)` rebuilds state after re-parameterization; numbers may differ slightly from Anki's Rust implementation `fsrs-rs` ([py-fsrs README Optimizer](https://github.com/open-spaced-repetition/py-fsrs)).
- License: **MIT** (Copyright © 2022 Open Spaced Repetition) ([py-fsrs LICENSE](https://raw.githubusercontent.com/open-spaced-repetition/py-fsrs/main/LICENSE), [pyproject classifier MIT](https://raw.githubusercontent.com/open-spaced-repetition/py-fsrs/main/pyproject.toml)).
- Weight: base runtime dependency is **only `typing-extensions`**, `requires-python >=3.10` ([py-fsrs pyproject.toml](https://raw.githubusercontent.com/open-spaced-repetition/py-fsrs/main/pyproject.toml)). The `optimizer` extra adds **`torch`, `numpy`, `pandas`, `tqdm`** ([py-fsrs pyproject.toml](https://raw.githubusercontent.com/open-spaced-repetition/py-fsrs/main/pyproject.toml)). *Analysis:* per-review scheduling is O(1) arithmetic (a handful of exp/pow ops per the FSRS-6 formulas above) with per-card state of two floats + due date — negligible CPU/RAM even on low-end hardware; the optimizer is the only heavy piece (PyTorch) and should run offline, not in the request path. (Vendor publishes no MB/CPU benchmark; weight claim is inferred from the dependency list, not a vendor measurement.)

### 2.3 `ts-fsrs` — API shape (frontend alternative, not recommended for v1)

- Install: `pnpm add ts-fsrs` (+ optionally `@open-spaced-repetition/binding` for parameter training/CSV); requires **Node >= 20** ([ts-fsrs README Installation](https://github.com/open-spaced-repetition/ts-fsrs)).
- Two packages in the monorepo: `ts-fsrs` (scheduler for review flows, FSRS v6) and `@open-spaced-repetition/binding` (optimizer/training + CSV conversion, FSRS v6) ([ts-fsrs README Packages table](https://github.com/open-spaced-repetition/ts-fsrs)).
- Minimal flow ([ts-fsrs README Basic Usage](https://github.com/open-spaced-repetition/ts-fsrs), [API docs](https://open-spaced-repetition.github.io/ts-fsrs/)):
  ```ts
  import { createEmptyCard, fsrs, Rating } from 'ts-fsrs';
  const scheduler = fsrs();
  const card = createEmptyCard();
  const preview = scheduler.repeat(card, new Date());      // all 4 outcomes
  const result = scheduler.next(card, new Date(), Rating.Good); // apply answer
  ```
- Tunables ([ts-fsrs docs Custom parameters](https://open-spaced-repetition.github.io/ts-fsrs/)): `request_retention` (0–1, = py-fsrs `desired_retention`), `maximum_interval`, `enable_fuzz`, `enable_short_term` + `learning_steps`/`relearning_steps` (string durations e.g. `'1m','10m'`). `generatorParameters()` builds a complete serializable `FSRSParameters` object; validate untrusted stored params (e.g. zod) at the app boundary before `fsrs(params)` ([ts-fsrs docs generatorParameters](https://open-spaced-repetition.github.io/ts-fsrs/)).
- Helpers: `get_retrievability()`, standalone `forgetting_curve()`, low-level `next_state()` + `next_interval()` for simulations, and `rollback()` / `forget()` / `reschedule()` for replay/persistence ([ts-fsrs docs Retrievability, next_state, History helpers](https://open-spaced-repetition.github.io/ts-fsrs/)).
- Supports ESM/CJS/UMD and browser/WASI ([ts-fsrs README About/packages](https://github.com/open-spaced-repetition/ts-fsrs)).
- License: **MIT** (Copyright © 2026 Open Spaced Repetition) ([ts-fsrs LICENSE](https://raw.githubusercontent.com/open-spaced-repetition/ts-fsrs/main/LICENSE)).
- Weight: scheduler itself is dependency-light TypeScript (no torch); the optimizer lives in the separate `binding` package (Rust/WASM). *Analysis:* same O(1)-per-review conclusion as py-fsrs applies; no vendor-published bundle-bytes figure was found — check `node_modules/ts-fsrs` unpacked size at integration time if a strict frontend budget exists.

### 2.4 SM-2 fallback — spec and package

- Authoritative spec (Wozniak 1987–89, SuperMemo 1.0–3.0) ([SuperMemo: Application of a computer… SM-2](https://www.supermemo.com/en/blog/application-of-a-computer-to-improve-the-results-obtained-in-working-with-the-supermemo-method)):
  1. Split knowledge into smallest items; initial E-Factor 2.5.
  2. Intervals `I(1)=1`, `I(2)=6`, `I(n)=I(n−1)·EF` for n>2 (days, round fractions up).
  3. Grade each repetition q ∈ 0–5 (5 perfect … 0 blackout).
  4. Update `EF′ = EF + (0.1 − (5−q)·(0.08 + (5−q)·0.02))`; q=4 leaves EF unchanged; clamp EF ≥ 1.3 (items below 1.3 repeat annoyingly and usually violate the minimum-information principle).
  5. If q<3, restart intervals from I(1) without changing EF.
  6. Same-day: repeat all items scoring <4 until they score ≥4.
- Python package `sm-2` (`pip install sm-2`): `Scheduler, Card, ReviewLog`, `scheduler.review_card(card, rating)` with 0–5 ratings, UTC-only datetimes, `to_dict()/from_dict()` persistence ([sm-2 README Quickstart/Usage/Serialization](https://github.com/open-spaced-repetition/sm-2)). License **MIT** ([sm-2 LICENSE](https://github.com/open-spaced-repetition/sm-2/blob/main/LICENSE)). *Analysis:* dependency surface is comparably tiny (same org template as py-fsrs); either vendoring the ~10-line EF update or depending on `sm-2` is defensible — vendoring wins if the project wants zero new BYO-scheduler deps.
- When to prefer SM-2 (*synthesis from the sources above*): cold start (no per-user history to fit 21 FSRS weights), hard-offline/embedded targets, or as a deterministic baseline to A/B against FSRS. Anki itself shipped SM-2 as the legacy scheduler with FSRS as the opt-in successor, and keeps SM-2-only options (graduating interval, easy bonus) hidden once FSRS is on ([Anki Background: SM-2 then FSRS](https://docs.ankiweb.net/background.html), [Anki Deck Options: FSRS section](https://docs.ankiweb.net/deck-options.html)).

---

## 3. Active recall + quiz generation (question types worth generating)

### 3.1 Why active recall

- Anki's manual grounds the method in two principles: **active recall** (attempt the answer before seeing it — recall itself strengthens memory; failure signals what to relearn) and **spaced repetition** (review only when about to forget; easier cards get longer intervals) ([Anki Manual: Background](https://docs.ankiweb.net/background.html)).
- Practical recall Edge: show question alone first; if no answer within ~10s, reveal and move on rather than grind ([Anki Manual: Studying/Questions](https://docs.ankiweb.net/studying.html)).

### 3.2 Question types to generate

1. **Basic Q/A (front→back, optionally back→front).** Anki's unit is the *note* (fields) from which one or more *cards* are generated; keep each card to one question ([Anki Manual: Adding Cards and Notes](https://docs.ankiweb.net/editing.html), [Anki Manual: Card Templates](https://docs.ankiweb.net/templates/intro.html)). *Analysis:* best default for definitions, people/dates, cause→effect, and "what/why/how" prompts the LLM can write from a source chunk.
2. **Cloze deletion (fill-the-blank, including overlapping/nested up to limits).** Anki's cloze note type hides `{{c1::…}}` spans (multiple deletions per sentence allowed; nested clozes supported since 2.1.56 with depth limits ~3–8); SuperMemo Rule 5 calls cloze "easy and effective" and the recommended on-ramp for beginners converting textbook prose into reviewable items ([Anki Manual: Cloze Deletion](https://docs.ankiweb.net/editing.html), [SuperMemo 20 rules via super-memory.com](https://super-memory.com/articles/20rules.htm)). *Analysis:* highest-yield generator for dense expository notes — one fact per blank (see §4).
3. **Image occlusion (diagram labeling).** Anki ≥23.10 supports native IO notes with Hide-All-Guess-One / Hide-One-Guess-One mask modes ([Anki Manual: Image Occlusion](https://docs.ankiweb.net/editing.html)). *Analysis:* generate only when the source note actually contains a diagram; skip for text-only v1.
4. **Multiple choice — formative only, never the retention vehicle.** Neither the Anki manual nor the SuperMemo 20-rules corpus recommends MC for durable memory (both push *recall*, including type-in-the-answer checks); recognition is easier to guess and leaks context ([Anki Manual: Background/Studying](https://docs.ankiweb.net/background.html), [Anki Manual: Card Templates/type-in-the-answer](https://docs.ankiweb.net/templates/intro.html), [SuperMemo 20 rules](https://super-memory.com/articles/20rules.htm)). *Synthesis:* use MC sparingly as a low-stakes self-check, then convert each stem into a cloze or basic card for the actual schedule.

### 3.3 Good vs bad flashcards (SuperMemo 20 rules + Anki editing guidance)

Primary rule list: Wozniak's "20 rules of formulating knowledge" ([full text at super-memory.com](https://super-memory.com/articles/20rules.htm), [summary at supermemo.com/blog](https://www.supermemo.com/en/blog/twenty-rules-of-formulating-knowledge)); Anki's editing chapter distills it to **"Keep it simple"** and **"Don't memorize without understanding"** ([Anki Manual: Effective Learning](https://docs.ankiweb.net/editing.html)). Rules 1–16 all revolve around making memories simple ([20-rules recap](https://super-memory.com/articles/20rules.htm)).

| # | Rule (encode in prompts) | Good | Bad |
|---|---|---|---|
| 1 | Do not learn what you don't understand | Card assumes the chapter was read; answer explains the *why* in the Extra field | Asking for a formula's symbols before the concept is understood |
| 2 | Learn before you memorize (big picture first) | Generate overview cards before detail cards | Atomic trivia with no context card |
| 4 | **Minimum information principle** — one fact per card, short Q, even shorter A | `Q: Nominative answers which questions? A: wer? was?` (per-rule example pattern) | "List all 4 German cases with questions and articles" on one card |
| 5 | Cloze deletion is easy and effective | `Canberra was founded in {{c1::1913}}` ([Anki cloze syntax](https://docs.ankiweb.net/editing.html)) | Deleting a whole clause instead of one term |
| 6–8 | Graphics help; avoid sets/enumerations >3–5 items; avoid lists | Split a 7-item list into 7 clozes or 2–3 chunk cards | "Name all 7 …" single card |
| 9–11 | Personalize, use examples, mnemonics for hard items | Concrete example + source cue | Abstract definition with no example |
| 13–17 | Interference: avoid similar-looking Qs together; redundancy from multiple angles is OK | Forward + reverse word cards; derivation-step cards | Five near-identical stems differing by one word, introduced same day |
| 19–20 | Date-stamp, prioritize (high-yield first) | `Last-verified: <date>` metadata; generate core cards before trivia | Undated facts that silently go stale |

Key anti-patterns to reject at generation time (all per the sources above): multi-fact cards, enumerations, uncontextualized lists/acronym mountains, and "recognition-only" MC stems with no recall twin.

---

## 4. Card-design rules → generation-prompt encoding (copy/paste seed)

> Encode the seven constraints below in every quiz/flashcard prompt. They are paraphrases of the cited rules, kept prompt-sized.

1. `ONE_FACT`: one retrievable fact per card; split compounds into N cards ([Rule 4](https://super-memory.com/articles/20rules.htm)).
2. `SHORT_A`: answers ≤ ~10 words where possible; prefer a term/date/name over a sentence ([Rule 4 "answers even shorter"](https://super-memory.com/articles/20rules.htm)).
3. `UNDERSTAND_FIRST`: refuse to card-ify text the source doesn't explain; emit a "needs context" flag instead ([Rules 1–2](https://super-memory.com/articles/20rules.htm), [Anki Effective Learning](https://docs.ankiweb.net/editing.html)).
4. `CLOZE_FIRST`: for sentences, prefer `{{cN::hidden}}` single-span clozes over Q/A; one hidden span per card ([Rule 5](https://super-memory.com/articles/20rules.htm), [Anki Cloze](https://docs.ankiweb.net/editing.html)).
5. `NO_LISTS`: never ask for >3 items in one answer; split enumerations ([Rules 7–8](https://super-memory.com/articles/20rules.htm)).
6. `REDUNDANT_OK`: may emit forward+reverse / rephrased twins for the same fact; do NOT emit near-duplicate stems in the same batch ([Rules 13–17](https://super-memory.com/articles/20rules.htm)).
7. `STAMP`: append `source_id` + `verified_date` to every card; order core-before-trivia ([Rules 19–20](https://super-memory.com/articles/20rules.htm)).

Suggested output schema per card: `{ front, back, cloze_text?, card_type: basic|cloze|mc_check, source_id, verified_date, atomic_fact }`. Review UI shows front only, requires an explicit recall attempt, then reveals back with Again/Hard/Good/Easy ([Anki Studying: Questions + Answer Buttons](https://docs.ankiweb.net/studying.html)).

---

## 5. Learning metrics — minimal set (and where to store them)

Anki's stats chapter defines the vocabulary; the table below maps it to the smallest set worth building ([Anki Manual: Card Info, Graphs and Statistics](https://docs.ankiweb.net/stats.html)).

| Metric (store per review) | Definition (Anki) | Why minimal | Implementation note |
|---|---|---|---|
| `retention` (pass rate) | True Retention table: Again=Fail; Hard/Good/Easy=Pass; mature = interval ≥21d; first review of day counts; compare against `desired_retention` (FSRS) | Answers "is scheduling working?" | Aggregate daily/weekly/monthly; noisy daily — judge monthly ([Stats: True Retention](https://docs.ankiweb.net/stats.html), [Deck Options: Desired Retention 90% default, keep <97%](https://docs.ankiweb.net/deck-options.html)) |
| `lapses` + `relearn_count` | Again-on-review = lapse; lapsed cards enter (re)learning steps; leeches flagged separately | Detects bad cards, not bad memory | Count Again presses; reuse Anki's leech idea to flag chronic failers for rewrite/split ([Studying: Answer Buttons](https://docs.ankiweb.net/studying.html), [Deck Options: Lapses/Relearning](https://docs.ankiweb.net/deck-options.html)) |
| `seconds_per_card` (median) | Revlog `time` column = ms spent on Q+A before grading; Review-Time graph mirrors Review-Count | Cost side of retention; spotsite slow cards | Record per-review ms; cap at 60s like Anki's internal timer; never let it influence scheduling ([Stats: Manual Analysis/revlog `time`](https://docs.ankiweb.net/stats.html), [Deck Options: Timers](https://docs.ankiweb.net/deck-options.html)) |
| `due / forecast / daily_load` | Future-Due graph: reviews due per future day assuming no new cards + no fails; `daily_load = Σ 1/I_n` | Capacity planning ("reviews/day if I add N cards") | Recompute from current intervals; FSRS simulator generalizes this ([Stats: Future Due](https://docs.ankiweb.net/stats.html), [Deck Options: Simulator](https://docs.ankiweb.net/deck-options.html)) |
| `streak_days` + `reviews/day` | Calendar graph of past activity; Reviews graph split by learn/young/mature/relearn | Habit loop; cheapest motivation metric | Derive from revlog timestamps; no extra instrumentation ([Stats: Calendar/Reviews](https://docs.ankiweb.net/stats.html)) |
| FSRS state (if FSRS on): `stability, difficulty, retrievability` | S = time for R to fall 100%→90%; D sets interval growth; estimated knowledge = mean R × reviewed cards | Powers forecast + "what to study" sort (ascending retrievability) | Persist per card from scheduler; expose Card Stability/Difficulty/Retrievability views ([Stats: FSRS graphs](https://docs.ankiweb.net/stats.html), [Deck Options: Review Sort/FSRS](https://docs.ankiweb.net/deck-options.html)) |

Supporting fields to log per review (Anki revlog schema, for SurrealDB table design): review epoch-ms `id`, card id `cid`, button `ease` (1=Again..4=Easy), new `ivl` / previous `lastIvl`, ease factor `factor`, `time` ms, card `type` (0 learning / 1 review / 2 relearn / 3 cram) ([Anki Manual: Manual Analysis](https://docs.ankiweb.net/stats.html)). Answer-button honesty matters: Good should be the modal grade (~80–95%), Again ~5–20%; pressing Hard instead of Again on forgotten cards inflates intervals and breaks FSRS calibration ([Anki Studying: Answer Buttons](https://docs.ankiweb.net/studying.html), [Anki Deck Options: FSRS Short Guide](https://docs.ankiweb.net/deck-options.html)).

Deliberately excluded from v1: hourly-breakdown heatmaps, per-deck ease histograms, PDF exports, simulator UI — all exist in Anki ([Stats chapter](https://docs.ankiweb.net/stats.html)) but are not needed to prove the loop works.

---

## 6. Open questions / follow-ups

1. Confirm SurrealDB card/review table shape against `Card.to_json()` / `ReviewLog.to_json()` fields before building the scheduler service (py-fsrs serialization contract: [JSON-Serialization](https://github.com/open-spaced-repetition/py-fsrs)).
2. Decide leech threshold (Anki default: 8 lapses → flag; see [Deck Options: Leeches](https://docs.ankiweb.net/deck-options.html)) vs simpler "3 Agains in 30d → suggest split".
3. If frontend due-preview is later required, spike `ts-fsrs` `repeat()` in the web client while keeping Python as scheduling source-of-truth ([ts-fsrs repeat vs next](https://open-spaced-repetition.github.io/ts-fsrs/)).

## Sources (primary, in order of reliance)

1. FSRS algorithm + defaults: <https://github.com/open-spaced-repetition/awesome-fsrs/wiki/The-Algorithm>
2. py-fsrs README/API: <https://github.com/open-spaced-repetition/py-fsrs> · scheduler docs <https://open-spaced-repetition.github.io/py-fsrs/fsrs.html> · pyproject <https://raw.githubusercontent.com/open-spaced-repetition/py-fsrs/main/pyproject.toml> · LICENSE <https://raw.githubusercontent.com/open-spaced-repetition/py-fsrs/main/LICENSE>
3. ts-fsrs README: <https://github.com/open-spaced-repetition/ts-fsrs> · API docs <https://open-spaced-repetition.github.io/ts-fsrs/> · LICENSE <https://raw.githubusercontent.com/open-spaced-repetition/ts-fsrs/main/LICENSE>
4. SM-2 spec (Wozniak): <https://www.supermemo.com/en/blog/application-of-a-computer-to-improve-the-results-obtained-in-working-with-the-supermemo-method> · sm-2 package <https://github.com/open-spaced-repetition/sm-2>
5. Anki Manual: Background <https://docs.ankiweb.net/background.html> · Studying <https://docs.ankiweb.net/studying.html> · Editing (incl. cloze + effective learning) <https://docs.ankiweb.net/editing.html> · Deck Options (FSRS) <https://docs.ankiweb.net/deck-options.html> · Stats <https://docs.ankiweb.net/stats.html> · Templates <https://docs.ankiweb.net/templates/intro.html>
6. SuperMemo 20 rules: <https://super-memory.com/articles/20rules.htm> · <https://www.supermemo.com/en/blog/twenty-rules-of-formulating-knowledge>
7. Benchmark: <https://github.com/open-spaced-repetition/srs-benchmark>
