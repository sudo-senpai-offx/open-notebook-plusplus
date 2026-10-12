# Flash storage + battery-aware scheduling research (32GB eMMC phones, old x86 HDD boxes)

Scope: single-user local `open-notebook`, SurrealDB (RocksDB backend) + SQLite-style
local persistence, background worker jobs. Two hostile environments:

- **Tier A — 32GB eMMC phones** (typically eMMC 5.1, shared with OS, low spare area,
  weak random-write IOPS).
- **Tier B — old x86 HDD boxes** (spinning disk, ~8ms seeks, sequential >> random).

Workload of interest: 30s heartbeat `UPDATE ... MERGE {heartbeat_at, progress, stage}`
on the **same record key** (`open_notebook/utils/job_control.py:39-77`, interval
`OPEN_NOTEBOOK_HEARTBEAT_SECONDS` default 30 at `job_control.py:116-120`), plus
embedding writes (vector-index maintenance) and periodic sync/checkpoint jobs.

Provenance note: RocksDB/Android/Linux-kernel/SQLite claims below are from primary
docs (RocksDB wiki, `sqlite.org`, `developer.android.com` reference + guides,
`docs.kernel.org`, `upower.freedesktop.org`). JEDEC eMMC/UFS specs are paywalled,
so raw IOPS/throughput numbers are labelled as **indicative vendor-published figures**,
not spec values. Treat them as order-of-magnitude inputs to tuning, not guarantees.

## 1. What the media actually costs you

### 1a. eMMC 5.1: random writes are the bottleneck, fsync is expensive

- eMMC 5.1 tops out at ~400 MB/s sequential (HS400) and is **half-duplex** — reads and
  writes cannot overlap. UFS uses a full-duplex serial interface with SCSI-style
  command queuing (up to 32 queued commands), which is why its random-IOPS lead is
  much larger than its sequential lead. (Vendor guides: Flexxon eMMC-vs-UFS comparison;
  VeloMax UFS overview; Ampheo embedded-Linux guide notes eMMC 5.1 does have a 32-deep
  command queue on capable hosts, but UFS + Linux `UFSHCD`/SCSI-midlayer integration is
  architected for queued random I/O.)
- Indicative vendor-published random performance (validate per part/density):
  eMMC random read ~8–15k IOPS, **random write ~0.5–2k IOPS**; UFS 2.1 random read
  ~40–60k IOPS, random write ~20–35k IOPS (Lexar Enterprise embedded guide).
  Sequential eMMC 5.1: ~270–330 MB/s read, ~25–200 MB/s write **capacity-dependent**
  (an 8GB part may sustain only ~25 MB/s write; 128GB ~200 MB/s) (Suntsu engineering
  guide, shipping-part figures).
- **What breaks first on eMMC:** small synchronous random writes. Each committing
  heartbeat that forces an `fsync` pays full flash-program + FTL-metadata latency on a
  device that can only do ~500–2000 random writes/s *shared with the whole OS*. A 30s
  heartbeat sounds harmless, but with `WriteOptions.sync=true` (RocksDB) or
  `synchronous=FULL` (SQLite) every tick is a durability round-trip, and on a nearly
  full 32GB phone (little spare area → heavier GC) tail latency spikes. Batching and
  relaxing per-commit sync (Sections 2–3) attack exactly this cost.
- eMMC link power management exists but actual energy must be measured per workload
  and platform — do not assume UFS-is-faster implies UFS-uses-less-energy (Ampheo
  embedded-Linux guide, Q&A on link power states/auto-hibernation).

### 1b. HDD: seeks dominate, sequential is 10–100x cheaper than random

- LSM trees (RocksDB) exist to turn random writes into large sequential writes:
  writes append **sequentially** to the WAL (`.log` files) in memory-buffered,
  kernel-buffered form, then memtables flush as large sorted SSTables. On a 7200 RPM
  HDD a seek costs ~8ms, so small random writes land at ~100–200 IOPS while sequential
  streaming reaches ~100–200 MB/s — a ~50–100x gap (DEV/Vivek Bansal sequential-vs-random
  writeups; BVLSM paper §1: "buffering updates in memory and appending them
  sequentially to disk … transforms random writes into large sequential writes,
  thereby minimizing disk seek time").
- **What breaks first on HDD:** anything that forces the head to seek per commit —
  frequent `fsync` on small WAL appends, SQLite rollback-journal mode (writes content
  twice, non-sequentially), and compaction reads scattered across levels competing
  with foreground WAL appends. `PRAGMA synchronous=NORMAL` in WAL mode exists partly
  for this: "if an application runs checkpoint in a separate thread, the main thread
  doing updates will never block on a sync operation. This helps to prevent
  'latch-up' in applications running on a busy disk drive" (`sqlite.org/wal.html`).
- SQLite WAL checkpoint cost is HDD-visible: default `wal_autocheckpoint = 1000`
  pages (~4MB) triggers copy-back of WAL content into the DB file — random-ish reads
  + writes across the DB file while foreground work continues (SQLite Forum checkpoint
  explainer; `sqlite.org/wal.html` § on 1000-page autocheckpoint and WAL recycling by
  overwrite-from-start rather than truncate).

### 1c. fsync/WAL cost model (both tiers, from primary docs)

- RocksDB `WriteOptions.sync=true` fsyncs the WAL file before returning
  (`WAL Performance` wiki, "Sync Mode"). Default WAL write path flushes the app-memory
  buffer to the OS page cache per write (`fwrite` syscall); `DBOptions.manual_wal_flush=true`
  defers even that until the buffer is full or `DB::FlushWAL()` is called — "not doing
  fwrite syscall after every Put" (RocksDB blog, "FlushWAL; less fwrite, faster
  writes"). In manual mode "the WAL write is not crash safe" until flushed
  (`WAL Performance` wiki).
- With default `recycle_log_file_num=false`, every fsync generates **≥2 I/Os** (data +
  metadata/size update); `fallocate()` reservation does not prevent the metadata I/O
  (`WAL Performance` wiki). Mitigation the wiki itself offers: `recycle_log_file_num>0`
  ("works more efficiently with `manual_wal_flush`") so WAL segments are reused.
- RocksDB defaults to `fdatasync` (not full `fsync`) on Linux — it can skip the
  directory-entry update, saving one I/O on WAL appends where only file size grows;
  `use_fsync` opts into the stronger guarantee (BeachDB WAL writeup summarizing
  RocksDB `WriteOptions::sync` / `use_fsync` behavior).
- SQLite WAL + `synchronous=NORMAL`: "the WAL file is synchronized before each
  checkpoint and the database file is synchronized after each completed checkpoint
  … but no sync operations occur during most transactions" (sqlite-users list,
  citing `sqlite.org` semantics) — i.e. commits between checkpoints are fast but a
  power loss can roll back recently committed transactions, **without** corrupting
  the DB. `synchronous=FULL` fsyncs the WAL after each transaction before COMMIT
  returns — durable but slower (same source; `blag/avi.im` SQLite-fsync chart:
  WAL + FULL + fullfsync = durable; WAL + NORMAL = fast, may lose tail on power
  loss). `SQLITE_DEFAULT_WAL_SYNCHRONOUS=1` builds even change the WAL-mode *default*
  to NORMAL (better-sqlite3 `docs/performance.md`).
- SQLite WAL issues "many fewer fsync() operations" than rollback-journal mode and is
  therefore "less vulnerable to problems on systems where fsync() is broken", with
  more-sequential I/O and no reader/writer blocking (`sqlite.org/wal.html`, points 1–4).

## 2. Storage-tier-specific knobs

All RocksDB option names below are from the RocksDB wiki (`RocksDB Tuning Guide`,
`Setup Options and Basic Tuning`, `Write Stalls`, `WAL Performance`) and the
`Options` struct docs (`docs.rs/rocksdb`); SurrealDB v2 maps a subset to
`SURREAL_ROCKSDB_*` env vars (see `research-surreal-lowmem.md` — byte-size vars take
**raw byte counts only** on v2, `32MiB` suffixes silently ignored until 3.0).

### Tier A — 32GB eMMC phone

| Knob | Value / direction | Rationale |
|---|---|---|
| Batch the 30s heartbeat; never `sync=true` per tick | Coalesce heartbeat+progress into one write per interval; `WriteOptions.sync=false`, rely on periodic `DB::FlushWAL(true)` / `SyncWAL()` | Each synced tick is a flash round-trip on a ~0.5–2k random-write-IOPS device shared with the OS. RocksDB `SyncWAL()` "will not block writes being executed in other threads" (`WAL Performance` wiki), so a background thread can sync without stalling ingest. |
| `manual_wal_flush=true` + timed `FlushWAL()` | Flush every N seconds or M KB, not per `Put` | Kills the per-`Put` `fwrite` syscall ("less fwrite, faster writes", RocksDB blog). Crash window = since last flush — acceptable for heartbeat/progress, not for user data (Section 4: split criticality). |
| `recycle_log_file_num` > 0 | e.g. 4–8 | Avoids the ≥2-I/Os-per-fsync metadata penalty of always-creating new WAL segments (`WAL Performance` wiki). |
| `wal_bytes_per_sync` / `bytes_per_sync` | e.g. 1MB (rate-limiter default) | Groups WAL/SST writes into larger sequential chunks; the rate limiter "automatically enables bytes_per_sync to 1MB" (`Options` docs). Larger sequential writes suit flash FTL page/block granularity. |
| `wal_dir` | **Same slow device** (no faster tier exists on a phone) | `wal_dir` "specifies the absolute path of the directory the WAL should be written to" (default: DB dir) (`Options::set_wal_dir`). Do **not** point WAL at external/adoptable SD — slower and less reliable than internal eMMC. The "WAL on faster media" trick is HDD-box-only. |
| `write_buffer_size` / `max_write_buffer_number` | Larger buffers, fewer flushes; e.g. 32–64MB × 3–4 (within the low-mem envelope from `research-surreal-lowmem.md`) | "Focus on flushing less by increasing buffer sizes or minimum buffers to merge, which automatically dedupes keys that are updated" (Quasar RocksDB tuning guide, citing the official guide) — heartbeat rewrites the **same key**, so bigger buffers = more in-memory dedupe = fewer flash writes. Watch total: `max_bytes_for_level_base` must be ≥ `max_write_buffer_number × write_buffer_size` or "all L0 files will be larger than what can fit in L1" (same source). |
| `min_write_buffer_number_to_merge=2` | Keep (Surreal default) | Merges two memtables before flush, deduping same-key overwrites (`Tuning Guide`, `Write Stalls` wiki; `research-surreal-lowmem.md` §2). |
| Compaction throttling: `level0_slowdown/stop_writes_trigger`, `soft/hard_pending_compaction_bytes_limit`, `delayed_write_rate` | Raise slowdown/stop triggers modestly; cap background IO via rate limiter | Stall triggers are per-CF but "write stalls apply to the whole DB" (`Write Stalls` wiki). On eMMC, a stall storm = UI jank; prefer smoothing (rate limiter, `bytes_per_sync`) over huge triggers that convert stalls into OOM/read-amp. Log lines "Stalling/Stopping writes because of …" identify the cause (Quasar guide). |
| `max_background_flushes` / `max_background_compactions` | 1 flush / 1–2 compactions | Fewer concurrent writers → fewer concurrent flash streams → less FTL GC churn on a half-duplex device. (`Tuning Guide` multi-DB example uses 1+1 for constrained boxes.) |
| SQLite side (job DB / caches) | `journal_mode=WAL`, `synchronous=NORMAL`, `wal_autocheckpoint` tuned (e.g. 2000–4000 pages), checkpoint on idle/charging | WAL+NORMAL = "many fewer fsyncs", sequential I/O (`sqlite.org/wal.html`); checkpoint is the only sync point, so schedule it when the phone is idle/charging (Section 3). Keep read txns short — long readers cause **checkpoint starvation** ("WAL file will grow without bound", SQLite Forum). |
| SQLite durability split | User data: `synchronous=FULL`; heartbeat/progress: `NORMAL` or separate DB | `blag/avi.im` chart: only FULL gives durability; NORMAL is explicitly "if durability is not a concern". Heartbeat state is reconstructible → NORMAL; user notes → FULL. |

### Tier B — old x86 HDD box

| Knob | Value / direction | Rationale |
|---|---|---|
| `wal_dir` on fastest spindle (or SSD if one exists) | Point `wal_dir` at the least-contended mount | Only tier where "WAL on faster media" is real. WAL is pure-sequential-append; isolating it from SST/compaction seeks removes the head-contention that causes latch-up. |
| `bytes_per_sync` / `wal_bytes_per_sync` | 1–4MB | On HDD, one 1MB sequential write beats 256×4KB seeks by ~2 orders of magnitude (§1b). Rate limiter auto-sets 1MB (`Options` docs). |
| `write_buffer_size` larger than phone tier | 64–128MB if RAM allows | Same dedupe logic as Tier A, plus: fewer, larger SST flushes = fewer seeks. Balance vs box RAM (block cache ~1/3 of budget, rest to page cache — `Setup Options and Basic Tuning`). |
| `target_file_size_base` / `max_bytes_for_level_base` | Scale with buffer (L1 ≥ buffers × size) | Per Quasar/official guide: keep `max_bytes_for_level_base ≥ max_write_buffer_number × write_buffer_size`. |
| Compaction scheduling | Single compaction thread; compact on idle; raise `level0_*_trigger` modestly | HDD compactions are seek-heavy and contend with WAL appends; one stream at a time, scheduled when the box is idle (Section 3, Linux side). Grep `LOG` for "Stopping/Stalling writes because of [reason]" to size triggers (Quasar guide). |
| SQLite | `journal_mode=WAL` (never rollback-journal on HDD: double writes + non-sequential), `synchronous=NORMAL` + checkpoint-on-idle, or FULL for user data | WAL "uses many fewer fsync() operations" and sequential I/O (`sqlite.org/wal.html`); NORMAL+separate-checkpoint-thread "helps prevent latch-up on a busy disk drive" (same page). |
| `use_fsync=false` (default) | Keep default `fdatasync` | Saves the directory-entry I/O per WAL append (BeachDB/RocksDB writeup). |

### Both tiers — write-batching rules (highest ROI, no knob needed)

1. **Coalesce the heartbeat.** One write per 30s tick carrying `{heartbeat_at, progress,
   stage}` already happens; ensure no *additional* per-tick writes (no separate progress
   row, no per-tick index update, no per-tick fsync). One CF stalling stalls the whole
   DB (`Write Stalls` wiki) — keep heartbeat and bulk ingest from fighting over the
   same CF's memtable budget.
2. **Group commit.** RocksDB's `WAL Performance` wiki documents group commit: concurrent
   writers share one sync. Batch embedding writes into fewer, larger `WriteBatch`es.
3. **Separate durability classes.** Heartbeat/progress (reconstructible) → unsynced or
   NORMAL; user content (irreplaceable) → synced/FULL. This is exactly the split the
   SQLite docs bless ("if durability is not a concern, NORMAL is normally all one
   needs in WAL mode" — `blag/avi.im` quoting doc intent; DRH caveat: NORMAL can still
   roll back the tail after power loss).

## 3. Battery-aware scheduling: integration points for a hibernate governor

### 3a. Android (phone tier) — primary API surface

**Detecting state (poll + push):**

- `BatteryManager.isCharging()` — "true if the battery is currently considered to be
  charging … plugged in and supplying sufficient power that the battery level is going
  up (or fully charged). Changes matched by broadcasts of `ACTION_CHARGING` /
  `ACTION_DISCHARGING`" (`BatteryManager` API reference; AOSP
  `platform/frameworks/base/.../BatteryManager.java`).
- Sticky `ACTION_BATTERY_CHANGED` intent extras: `EXTRA_STATUS` (charging/discharging/
  full), `EXTRA_LEVEL`/`EXTRA_SCALE`, `EXTRA_PLUGGED` (USB/AC/wireless) — the documented
  way to read level without registering a receiver (`battery-monitoring` guide pattern).
- `PowerManager.isPowerSaveMode()` + `ACTION_POWER_SAVE_MODE_CHANGED` — Battery Saver
  state; `PowerManager` also exposes thermal/location saver modes (API reference).
  Governor rule: saver ON → only critical work; saver + discharging → hibernate all
  heavy jobs.

**Deferring work declaratively (preferred — let the OS batch it):**

- WorkManager `Constraints`: `setRequiresCharging(true)` ("work will only run when the
  device is charging"), `setRequiresBatteryNotLow(true)` ("will not run if the device
  is in low battery mode"), plus network/idle/storage constraints; constraints apply to
  `PeriodicWorkRequest` — a run whose conditions aren't met is delayed/skipped
  (`Define work requests` guide). This is the guide-recommended replacement for manual
  battery checks: "use WorkManager with a `BatteryNotLow` constraint" (`battery-monitoring`
  guide).
- Under the hood WorkManager uses `JobScheduler` when the app isn't visible, so workers
  inherit job resource limits (`power-details` note). `JobScheduler`/`JobInfo` exposes
  the same primitives imperatively: `setRequiresCharging`, `setRequiresDeviceIdle`,
  `setRequiresBatteryNotLow` (`background-optimization` guide).

**Hard constraints the governor must respect (Doze + App Standby + buckets):**

- Doze (screen off, stationary, unplugged, API 23+): suspends network, ignores wake
  locks, defers `JobScheduler`/WorkManager, sync adapters, Wi-Fi scans, and standard
  `AlarmManager` (`setExact`/`setWindow`) to maintenance windows; windows get rarer the
  longer idle ("schedules maintenance windows less frequently"). Only
  `setAndAllowWhileIdle` / `setExactAndAllowWhileIdle` (≤1 per 9 min per app) and
  `setAlarmClock` escape; `isIgnoringBatteryOptimizations()` reports exemption, but
  Play policy forbids requesting it unless Doze "breaks the core function"
  (`doze-standby` guide, verbatim restrictions + checklist).
- **Design consequence:** a 30s heartbeat *cannot* run every 30s in Doze via
  WorkManager/JobScheduler — it will be deferred to maintenance windows. The governor
  must define Doze behavior explicitly: pause heartbeat in Doze and resume on exit
  (detect via `ACTION_CHARGING`/screen-on or WorkManager expedited on maintenance),
  or use `setAndAllowWhileIdle` (coarse, 9-min floor — not 30s). Do not fight Doze
  with exact alarms; the guide says use FCM high-priority only for user-visible
  notifications.
- App Standby buckets cap background execution even outside Doze: rare bucket ≈ 10 min
  rolling-24h, restricted ≈ 5 min rolling-24h / 1 job per day; charging relaxes limits
  (`power-details` resource-limits tables). Foreground service is the escape hatch but
  only for user-expected immediate work (`doze-standby`: "don't start a foreground
  service just to prevent idle").
- Test matrix (from the guide, automatable): `adb shell dumpsys deviceidle force-idle`
  / `unforce`, `dumpsys battery reset` for Doze; `dumpsys battery unplug` + `am
  set-inactive <pkg> true/false` + `am get-inactive` for App Standby (`doze-standby`
  §Test). Also `Test power-related issues` / Batterystats + Battery Historian for drain
  attribution (`Profile battery usage with Batterystats and Battery Historian`).

**Proposed governor states (Android):**

| State | Entry condition | Allowed work |
|---|---|---|
| `ACTIVE` | Screen on or recent use, saver off | Everything; heartbeat 30s. |
| `IDLE_DEFERRED` | Screen off / Doze / standby bucket, discharging | Heartbeat paused or ≥15-min coalesced; no embeddings/compaction; WorkManager with `BatteryNotLow` only. |
| `CHARGING_OPPORTUNISTIC` | `isCharging()` true | Drained backlog: embeddings, compaction-triggering bulk writes, SQLite `wal_checkpoint(RESTART/TRUNCATE)`, uploads. |
| `SAVER_HIBERNATE` | `isPowerSaveMode()` true, or battery low | Critical user writes only, `sync=false`/NORMAL, no background jobs. |

### 3b. Linux (x86 HDD-box tier) — primary API surface

- **Kernel truth:** `/sys/class/power_supply/<BATx>/status` (`Charging`/`Discharging`/`Full`),
  `capacity`, `charge_now`/`charge_full`, `current_now`; AC online at
  `/sys/class/power_supply/<AC>/online` (`docs.kernel.org/power/power_supply_class.html`:
  `STATUS` = "charging, full, discharging (i.e., powering a load), etc."). Poll these
  from the governor (cheap sysfs reads; note one Arch report of 100ms `status` reads
  while discharging on specific EC firmware — measure on target, prefer event-driven
  below if affected).
- **Desktop/daemon truth:** UPower D-Bus: device `State` enum (0 Unknown, 1 Charging,
  2 Discharging, 3 Empty, 4 Fully-charged, 5 Pending-charge, 6 Pending-discharge),
  `Percentage`, `EnergyRate`, `Online` for line-power (`upower.freedesktop.org/docs/Device.html`).
  CLI equivalents: `upower -e`, `upower -i <device>`, `upower --monitor(-detail)` for
  events (Linux Journal `upower` howto; Baeldung battery-status tutorial). Caveat: UPower
  can lag kernel/ACPI by minutes on some stacks (Red Hat BZ#1960997) — for
  charge-edge detection prefer sysfs or `upower --monitor` + debounce, and cross-check
  `energy-rate`.
- **No Doze equivalent:** Linux has no OS-enforced background deferral; the governor
  must self-throttle (systemd timers with `OnIdle`/`ConditionACPower=`, or its own
  poll of sysfs/UPower). Map Android states 1:1 but drive them from sysfs/UPower:
  `CHARGING_OPPORTUNISTIC` when `status ∈ {Charging, Full}` or `AC online=1`;
  `SAVER_HIBERNATE` when `capacity` < threshold (e.g. 20%) and discharging.

## 4. What to measure (before/after every knob)

| Signal | How | Target / interpretation |
|---|---|---|
| **Write amplification** | RocksDB `LOG` + statistics: bytes-written-to-WAL vs bytes-flushed/compacted; SQLite: WAL file growth vs checkpointed bytes | Heartbeat same-key dedupe should show WA → ~1 for that key range with merged memtables (§2). Rising WA after raising triggers = read-amp/write-amp debt, not a win. |
| **Stall lines** | Grep RocksDB `LOG` for `Stalling writes because of …` / `Stopping writes because of …` + reason (memtable count / L0 files / pending-compaction-bytes) | Zero `Stopping` on both tiers; `Stalling` only during bulk ingest. Each line names the exact trigger to resize (Quasar guide procedure; `Write Stalls` wiki taxonomy). |
| **fsync rate + latency** | `strace -c -e fsync,fdatasync -p <pid>` during heartbeat; SQLite: count syncs via VFS tracing or `wal_checkpoint` timing | Heartbeat ticks should contribute ~0 fsyncs (NORMAL/manual-flush); only periodic `SyncWAL`/checkpoint syncs. Tail latency on eMMC should drop with `recycle_log_file_num` + `bytes_per_sync`. |
| **Checkpoint cost** | Time `PRAGMA wal_checkpoint(RESTART)` (SQLite) and RocksDB flush+compact durations; WAL dir size over time | Checkpoint must fit inside `CHARGING_OPPORTUNISTIC` windows; unbounded WAL growth = checkpoint starvation from long readers (SQLite Forum) — fix readers, not checkpoint frequency. |
| **Drain rate** | Android: Batterystats + Battery Historian (`energy-rate`, µAh deltas per job); Linux: `energy_rate` (UPower) or `current_now × voltage_now` | Attribute mAh per: heartbeat tick, embedding batch, compaction, checkpoint. Governor success = background drain dominated by `CHARGING_OPPORTUNISTIC` windows. |
| **Doze conformance** | `dumpsys deviceidle` forced-idle runs; verify heartbeat pauses/resumes, no ANR, backlog drains on exit | From `doze-standby` test procedure. Also verify standby-bucket behavior (`am set-inactive`) for rarely-opened installs. |
| **Media headroom** | Free space on 32GB eMMC (spare-area proxy); HDD await/util via `iostat -x` during checkpoint/compaction | eMMC <10% free → expect GC tail spikes regardless of knobs (free space first). HDD `await` spike during checkpoint = reschedule to idle. |

## 5. Recommended default profile (shipped config starting point)

- **Heartbeat:** single coalesced unsynced write per tick; `manual_wal_flush` (RocksDB)
  / `synchronous=NORMAL` (SQLite job DB); durability split per §2 (user data FULL/synced).
- **eMMC phone:** buffers 32–64MB × 3–4, `min_write_buffer_number_to_merge=2`,
  `recycle_log_file_num` 4–8, `bytes_per_sync` 1MB, 1 flush + 1–2 compaction threads,
  SQLite autocheckpoint 2000–4000 pages fired from `CHARGING_OPPORTUNISTIC`.
- **HDD box:** `wal_dir` isolated, buffers 64–128MB, `bytes_per_sync` 1–4MB, single
  compaction stream, checkpoint/compact on idle (governor `IDLE`→`CHARGING` analog via
  sysfs/UPower AC state).
- **Governor:** 4 states (§3a table) driven by `BatteryManager`/`PowerManager` on
  Android, sysfs+UPower on Linux; WorkManager/`JobScheduler` constraints
  (`RequiresCharging`, `BatteryNotLow`, idle) as the enforcement mechanism on Android,
  self-throttle + systemd conditions on Linux. Never request battery-optimization
  exemption (Play policy) unless Doze breaks a core user-facing function.

## Sources

RocksDB (primary — wiki/blog/API docs):
- `RocksDB Tuning Guide` wiki — `write_buffer_size`/`max_write_buffer_number` examples
  (32MB×2; 64MB×3 + `target_file_size_base` 64MB, `max_background_compactions` 4,
  `level0_*_trigger` 8/17/24), `bytes_per_sync = 2MB` WAL setting.
- `Setup Options and Basic Tuning` wiki — default write buffer 64MB; block cache ~1/3
  of budget, rest to page cache; suggested starter options.
- `Write Stalls` wiki — stall taxonomy (immutable memtables ≥ max → stop; ≥ max−1 →
  slow when max>3; L0 slowdown/stop triggers; pending-compaction-bytes soft/hard →
  throttle to `delayed_write_rate`); stalls are per-CF-triggered but DB-wide.
- `WAL Performance` wiki — `sync=true` fsyncs before return; `manual_wal_flush`
  keeps WAL in memory (not crash-safe); `SyncWAL()` non-blocking; `recycle_log_file_num`
  avoids ≥2-I/Os-per-fsync; group commit.
- RocksDB blog `FlushWAL; less fwrite, faster writes` (2017-08-25) — per-`Put` flush
  → OS buffer, `manual_wal_flush` + `FlushWAL(bool sync)` API, MyRocks case.
- `Options` struct docs (`docs.rs/rocksdb`) — `wal_dir`, `wal_bytes_per_sync` (default
  0/off), `manual_wal_flush` (default false), rate-limiter ⇒ `bytes_per_sync` 1MB.
- Quasar `RocksDB Tuning Guide` (secondary, cites official guide) — "flush less …
  dedupes keys updated in short timespans"; `max_bytes_for_level_base ≥
  max_write_buffer_number × write_buffer_size`; stall-log grep procedure.

SQLite (primary):
- `sqlite.org/wal.html` — WAL faster (single sequential write), fewer fsyncs,
  reader/writer concurrency; NORMAL+separate-checkpoint-thread avoids latch-up on busy
  disks; autocheckpoint ~1000 pages (~4MB), recycle-by-overwrite.
- `sqlite-users` list (`WAL, synchronous=1 and durability`) — NORMAL: WAL synced before
  each checkpoint, DB after, no per-txn sync; FULL: fsync per txn before COMMIT return.
- better-sqlite3 `docs/performance.md` — `SQLITE_DEFAULT_WAL_SYNCHRONOUS=1` makes WAL
  default NORMAL ("extreme performance, slight durability loss"); override with
  `synchronous=FULL`.
- `blag/avi.im` SQLite-fsync chart — durability matrix (DELETE+EXTRA / WAL+FULL =
  durable; WAL+NORMAL = fast, tail loss on power failure).

Android (primary):
- `Optimize for Doze and App Standby` (`developer.android.com/training/...`) —
  restrictions, maintenance-window backoff, `setAndAllowWhileIdle` 9-min floor,
  `isIgnoringBatteryOptimizations()`, Play exemption policy, adb test procedure.
  (Fetched full text 2026-10-12.)
- `Define work requests` / `Background work` guides — `Constraints`
  (`RequiresCharging`, `BatteryNotLow`, network/idle), periodic-work deferral semantics;
  WorkManager persists scheduled work in its own SQLite DB.
- `Monitor the Battery Level and Charging State` guide — `BatteryNotLow` constraint
  recommendation; `ACTION_CHARGING`/`ACTION_DISCHARGING` halves of the pattern.
- `BatteryManager` API reference + AOSP `BatteryManager.java` — `isCharging()`
  semantics ("plugged in and level going up, or full").
- `Power management resource limits` (`power-details`) — standby-bucket execution caps
  (rare 10 min/24h, restricted 5 min/24h–1/day), charging relaxations, JobScheduler
  backing.
- `Background optimization` guide — `JobInfo` (`setRequiresCharging`,
  `setRequiresDeviceIdle`, `setRequiresBatteryNotLow`); foreground-service misuse rule.

Linux (primary):
- `docs.kernel.org/power/power_supply_class.html` — `STATUS` (charging/full/discharging),
  `CHARGE_TYPE`, sysfs attribute model.
- UPower `Device` reference (`upower.freedesktop.org/docs/Device.html`) — `State`
  enum 0–6, `Percentage`, `EnergyRate`; `upower -e/-i/--monitor` CLI surface.
- Red Hat BZ#1960997 (caveat) — UPower can lag kernel/ACPI by minutes; cross-check
  sysfs for edge detection.

Storage tiers (indicative vendor figures — not JEDEC primaries):
- Lexar Enterprise UFS-vs-eMMC guide — random read/write IOPS bands per tier.
- Suntsu engineering guide — sequential table (eMMC 5.1 270–330/25–200 MB/s
  capacity-dependent; UFS generations to 4.0).
- Flexxon comparison; VeloMax CQ/half-vs-full-duplex overview; Ampheo embedded-Linux
  guide (eMMC 5.1 CQ-32 note, UFSHCD/SCSI-midlayer, link-power measurement warning).
- HDD seek economics: DEV/Bansal sequential-vs-random writeups; BVLSM paper (LSM
  sequentialization); SQLite Forum checkpoint explainer (SSD-vs-HDD WAL behavior,
  starvation, RESTART semantics).
