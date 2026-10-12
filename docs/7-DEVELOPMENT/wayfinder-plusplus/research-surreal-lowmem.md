# SurrealDB v2 (RocksDB) on 512MB / 1c — low-memory research

Scope: single-user local `open-notebook`, RocksDB backend, 512MB/1c envelope.
Workload: heartbeat + progress `UPDATE command MERGE {heartbeat_at, progress, stage}` every
30s (`open_notebook/utils/job_control.py:39-77`, interval `OPEN_NOTEBOOK_HEARTBEAT_SECONDS`
default 30 at `job_control.py:116-120`) on the **same record key**, plus embedding writes
(vector-index maintenance). Current server launch: `surreal start --log trace ... rocksdb:/mydata/mydatabase.db`
(`supervisord.surrealdb.conf:5`, `docker-compose.yml:4`).

All knob names/defaults below are from the SurrealDB env-var reference; all RocksDB
mechanisms are from the RocksDB wiki. v2 caveat applies throughout: **on 2.x, byte-size
env vars take a raw byte count only — a suffixed value like `32MiB` is silently ignored
and the default is kept** (suffixes only work from 3.0.0).

## 1. Block cache sizing for sub-GB

- SurrealDB exposes `SURREAL_ROCKSDB_BLOCK_CACHE_SIZE`, default
  `max((system memory / 2) - 1 GiB, 16 MiB)`. On a 512MiB box the formula goes
  negative, so the effective default is the **16MiB floor**. The docs' own sizing
  table estimates ~80MiB total RocksDB memory on a 512MiB/1GiB instance.
- RocksDB guidance (`Setup Options and Basic Tuning`): block cache should be ~**1/3 of
  the total memory budget**, remainder left for OS page cache. RocksDB is two-tiered:
  block cache holds **uncompressed** blocks, OS page cache holds the **compressed** file
  blocks underneath.
- Counter-intuitive result documented in `Memory usage in RocksDB`: **shrinking block
  cache does not increase IO** — freed memory becomes page cache — but CPU rises
  (decompression on every page-cache hit). On 1c, that CPU-for-RAM trade is usually
  acceptable; on a read-heavy vector workload it is not.
- Indexes/filter blocks live **outside** the block cache by default and are bounded by
  `max_open_files`, not by the cache size ("you allocate 10GB for block cache but
  RocksDB uses 15GB — the difference is index/bloom blocks"). So a small block cache
  alone does not bound total memory; `SURREAL_ROCKSDB_MAX_OPEN_FILES` (default 1024)
  must come down too.
- `OptimizeForSmallDb` (in `options.h`) exists precisely for DBs under ~1GB: "you don't
  want to spend lots of memory for memtables."

Recommended 512MB values (raw integers for v2):

| Knob | Value (v2 raw bytes) | Rationale |
|---|---|---|
| `SURREAL_ROCKSDB_BLOCK_CACHE_SIZE` | `33554432` (32MiB) | 2x the 16MiB floor; ~1/3 of a ~100MiB RocksDB sub-budget. Range 16–48MiB defensible; above 64MiB starves app + page cache. |
| `SURREAL_ROCKSDB_MAX_OPEN_FILES` | `256` (512 max) | Bounds index/filter table-reader memory; 1024 default is for multi-GB servers. |
| `SURREAL_HNSW_CACHE_SIZE` | `33554432` (32MiB), or `16777216` if vector search is cold | Default is **256MiB** — larger than half the box. Mandatory cut. Note it bounds only cached element **vectors**; the HNSW adjacency graph stays resident **outside** this budget. |
| `SURREAL_ROCKSDB_BLOCK_SIZE` | keep `65536` default | Larger blocks = smaller index; do not drop to 4K on low-mem (index growth). |

## 2. Write-buffer / stall behavior under heartbeat load

Mechanism (`RocksDB Tuning Guide`, `Write Stalls` wiki):

- Writes land in the active memtable (`write_buffer_size` each); when full it goes
  immutable and a new one is created. Stall triggers: (a) immutable count ≥
  `max_write_buffer_number` → **stop**; (b) count ≥ `max-1` (when max > 3) → **slow**;
  (c) L0 file count ≥ `level0_slowdown/stop_writes_trigger` → slow/stop;
  (d) pending compaction bytes ≥ soft/hard limits → slow/stop, throttled to
  `delayed_write_rate`. One CF triggering a stall stalls the whole DB.
- `min_write_buffer_number_to_merge` (Surreal default 2): with 2, two memtables are
  merged and flushed together. Merging **dedupes same-key overwrites in memory** —
  directly relevant because every heartbeat rewrites the same `command:<id>` key.
- Mitigation directions from the wiki: flush backlog → more flush threads / larger
  `max_write_buffer_number`; compaction backlog → larger `write_buffer_size`,
  larger `min_write_buffer_number_to_merge`, more background jobs. Bigger memtables
  cut write amplification but cost memory and recovery time.

Why heartbeats rarely hard-stall but still hurt: 1 write/30s never fills memtables
fast enough to hit the memtable-count stall. The damage is **accumulated obsolete
versions**: each heartbeat is a separate transaction → WAL record + memtable entry +
(if sync) fsync. Obsolete versions compact from L0→L1→…; write amplification is
defined as **bytes written to storage / bytes written to the DB**, and production
LSM write amp in the 10–30x range is documented in the RocksDB evolution paper.
Steady trickle + embedding flushes grows L0 file count toward the slowdown trigger
(default file-compaction trigger 4 via `SURREAL_ROCKSDB_FILE_COMPACTION_TRIGGER`)
and keeps one compaction thread permanently warm on 1c.

Recommended 512MB/1c values:

| Knob | Value | Rationale |
|---|---|---|
| `SURREAL_ROCKSDB_WRITE_BUFFER_SIZE` | `16777216` (16MiB; default on <1GiB hosts is 32MiB) | Halves per-memtable memory; with count 2 → ≤32MiB memtable worst case. Matches small-DB pattern. |
| `SURREAL_ROCKSDB_MAX_WRITE_BUFFER_NUMBER` | `2` (keep <4GiB default) | Do not raise on 1c: more buffers = more memory + every `Get` scans all immutables linearly. |
| `SURREAL_ROCKSDB_MIN_WRITE_BUFFER_NUMBER_TO_MERGE` | `2` (keep default) | Keeps the same-key dedupe benefit for heartbeat overwrites. |
| `SURREAL_ROCKSDB_JOBS_COUNT` | `2` | Default is CPUs×2; pin it so a CPU-count change doesn't add threads. |
| `SURREAL_ROCKSDB_THREAD_COUNT` | `1` | Flush/compaction threads on 1c; more threads = more concurrent compaction memory. |
| `SURREAL_ROCKSDB_MAX_CONCURRENT_SUBCOMPACTIONS` | `1` (default 4) | Subcompactions parallelize one compaction; 4 is for big iron. |
| `SURREAL_ROCKSDB_COMPACTION_READAHEAD_SIZE` | `1048576` (1MiB; default 4MiB under 4GiB) | Smaller readahead = smaller compaction read buffers; slightly slower compaction. |
| `SURREAL_ROCKSDB_TARGET_FILE_SIZE_BASE` | keep `67108864` (64MiB) | Fewer, larger SSTs = fewer files/open handles on tiny disk. |
| `SURREAL_ROCKSDB_ENABLE_BLOB_FILES` / `MIN_BLOB_SIZE` | keep `true` / `4096` | Embedding-size values go to blob files, stay out of the LSM merge path. Disabling raises write amp for this workload. |
| `SURREAL_ROCKSDB_COMPACTION_STYLE` | keep `level` | `universal` cuts write amp but doubles transient space amp — dangerous on small disks. |

## 3. SurrealDB startup flags / env knobs that bound memory

Server flags (current launch uses `--log trace` — change this first):

- `--log info` (not `trace`): trace logging costs CPU, I/O, and log-buffer memory
  continuously; `trace` is never appropriate for a 512MB steady-state box.
- Storage path selects the engine: `rocksdb:/mydata/mydatabase.db` (already correct;
  do not mix backends in one directory).
- Bind/auth as today: `--bind 127.0.0.1:8000 --user/--pass`.

Memory-bounding env knobs (raw bytes on v2):

| Knob | Suggested 512MB value | Notes |
|---|---|---|
| `SURREAL_ROCKSDB_BLOCK_CACHE_SIZE` | `33554432` | See §1. Must stay **below** `SURREAL_MEMORY_THRESHOLD` if that guard is enabled (docs warn: threshold at/below cache size + full cache refuses every query until restart). |
| `SURREAL_HNSW_CACHE_SIZE` | `33554432` (or 16MiB) | Single biggest default (256MiB) relative to the box. Graph memory stays outside it — measure RSS regardless. |
| `SURREAL_MEMORY_THRESHOLD` (3.3+; verify presence on your v2 build — if absent, enforce via cgroup) | `419430400` (400MiB) | Guard refuses queries above tracked memory (allocator-reserved + block cache, **less than RSS**). Set below the 512MiB container limit, above block cache. Unset/0 disables. |
| `SURREAL_RUNTIME_WORKER_THREADS` | `1`–`2` | Default is core count (min 4) — 4 workers oversubscribe 1c. |
| `SURREAL_RUNTIME_MAX_BLOCKING_THREADS` | `16`–`32` (default 512) | Caps blocking-pool thread stacks (default stack 10MiB each). |
| `SURREAL_MAX_CONCURRENT_TASKS` | `8`–`16` (default 64) | Bounds buffered worker-channel jobs. |
| `SURREAL_TRANSACTION_CACHE_SIZE` | `512` | Pre-3.1 default was 10,000 — if your v2 build carries the old default, this is a large per-transaction cache; pin 512. |
| `SURREAL_DATASTORE_CACHE_SIZE` | `100`–`200` (default 1000) | Cross-transaction definition cache. |
| `SURREAL_EXTERNAL_SORTING_BUFFER_LIMIT` | `5000`–`10000` (default 50000) | Records before on-disk sort; lower = less query working memory. |
| `SURREAL_WEBSOCKET_MAX_MESSAGE_SIZE` | keep `134217728` unless embeddings are chunked smaller | Only lower if clients never send large frames; too low breaks embedding upserts. |
| Sync/durability (v2 path) | keep `SURREAL_ROCKSDB_GROUPED_COMMIT=true` (default); leave `BACKGROUND_FLUSH=true`, `BACKGROUND_FLUSH_INTERVAL=200` | Grouped commit batches multiple commits into one fsync — the available write batching **inside** the engine. (v3 renamed this to `SURREAL_DATASTORE_SYNC_DATA` duration; on v2 use the `BACKGROUND_FLUSH*` pair.) |
| Container | `--memory=512m --cpus=1` (and `--memory-swap=512m`) | The only hard bound; SurrealDB knobs are soft budgets underneath it. |
| Allocator (operational, not SurrealDB docs) | `MALLOC_CONF=background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000` | jemalloc decay profile used in reported low-OOM SurrealDB deployments; returns freed pages faster on constrained boxes. |

## 4. Does batching progress updates / lowering heartbeat frequency help?

Yes — materially, and it is the cheapest lever available. Reasons, all from primary sources:

1. **Fewer commits = fewer WAL records + fewer fsyncs.** With sync enabled each
   heartbeat transaction pays a WAL write and (grouped or not) fsync participation.
   30s → 120s is 4x fewer commits (2880 → 720/day per running command), independent
   of RocksDB tuning.
2. **Same-key collapse.** `min_write_buffer_number_to_merge=2` merges memtables
   before flushing and drops duplicate keys. Coalescing heartbeat+progress+stage
   into one `MERGE` per tick (already the code shape in `report_progress`) means one
   key version per tick instead of several; longer ticks mean fewer obsolete versions
   per flush, i.e. lower write amplification by definition
   (storage bytes / DB bytes).
3. **Compaction debt is super-linear in version count.** Every obsolete heartbeat
   version is rewritten down the levels until dropped. Halving tick rate roughly
   halves obsolete-version bytes entering L0; fewer L0 files also keeps distance
   from the `level0_slowdown_writes_trigger` throttle.
4. **Index maintenance per write.** If the `command` table carries full-text/HNSW
   indexes, each tick pays index maintenance (indexing batch default 250). Batching
   progress (e.g. write only on stage change or ≥5% progress delta, heartbeat-only
   ticks otherwise, or a single 60–120s tick) cuts index write batches proportionally.

Concrete app-side changes (no engine tuning needed):

- `OPEN_NOTEBOOK_HEARTBEAT_SECONDS=30` → `120` (env-only, already supported).
- In `report_progress`, skip the `UPDATE` when neither `progress` nor `stage` changed
  and only `heartbeat_at` would be rewritten more often than the stale threshold
  requires (stale check is `job_control.py:200+` / `lease_guard.py`; size the tick to
  a fraction of the stale timeout, not faster).
- Never fire heartbeat and progress as separate statements — one `MERGE` per tick.
- Keep embedding upserts out of the heartbeat path so large values don't re-enter
  the LSM on every tick (blob files mitigate, separation eliminates).

Expected magnitude: tick-rate reduction scales commit/WAL/fsync load **linearly**
(4x at 120s) and compaction/index load **linearly to better** (dedupes combine with
fewer versions). It will not fix HNSW graph residency — that memory is held outside
all write-path caches and must be addressed via `SURREAL_HNSW_CACHE_SIZE` + measuring
RSS + possibly deferring index build to job end.

## 5. What to measure

- **Container truth:** `cat /sys/fs/cgroup/memory.current` (or `docker stats`), plus
  `ps -o rss,vsz -p <surreal-pid>`. Compare against the 512MiB limit; tracked-memory
  guards read **below** RSS by design.
- **Stall signals:** RocksDB `LOG` under the data dir — `Stalling writes because…` /
  `Stopping writes because…` (memtable count, L0 file count, pending compaction
  bytes), and the periodic `Compaction Stats` block: `W-Amp` column, `Comp(sec)`,
  L0 file counts. Any nonzero `Interval stall` / growing L0 count = compaction
  behind writes.
- **Memory properties:** `rocksdb.block-cache-usage` (vs `SURREAL_ROCKSDB_BLOCK_CACHE_SIZE`),
  `rocksdb.cur-size-all-mem-tables` (vs write-buffer budget: size × count),
  `rocksdb.estimate-table-readers-mem` (index/filter vs `MAX_OPEN_FILES` cut).
- **Guard signal:** `The query was not executed due to the memory threshold being reached`
  means `SURREAL_MEMORY_THRESHOLD` is tripping — raise threshold or lower caches,
  never set threshold ≤ block cache size.
- **Write-amp check:** `iostat` disk write bandwidth ÷ DB ingest rate (Tuning Guide
  method), or `rocksdb.stats` cumulative compaction write vs flush write.
- **Before/after protocol:** fix workload (1 running command, embeddings on), record
  RSS + `memory.current` + W-Amp at 30s ticks for 30 min, then repeat at 120s ticks
  with the §2–§3 env applied. Accept if RSS p95 drops and L0 stays < slowdown trigger
  with no stall lines.

## Sources

- SurrealDB env reference (all `SURREAL_*` defaults, RocksDB table with 512MiB≈80MiB
  row, block-cache formula, HNSW/DiskANN cache semantics, `SURREAL_MEMORY_THRESHOLD`
  guard, byte-suffix v3-only warning):
  `https://surrealdb.com/docs/surrealdb/cli/env` and
  `https://surrealdb.com/docs/reference/cli/surrealdb-cli/environment-variables`
- SurrealDB `surreal start` reference (flags, `rocksdb://` engine selection, `--log`):
  `https://surrealdb.com/docs/reference/cli/surrealdb-cli/commands/start`
- RocksDB `Setup Options and Basic Tuning` (1/3-memory rule, write-buffer budgeting,
  `write_buffer_size` default 64MB):
  `https://github.com/facebook/rocksdb/wiki/Setup-Options-and-Basic-Tuning`
- RocksDB `Memory usage in RocksDB` (two-tier cache, index/filter outside block cache
  bounded by `max_open_files`, memtable vs L1 sizing, property names):
  `https://github.com/facebook/rocksdb/wiki/Memory-usage-in-RocksDB`
- RocksDB `Block Cache` (default 32MB LRU, shared-cache pattern):
  `https://github.com/facebook/rocksdb/wiki/Block-Cache`
- RocksDB `Tuning Guide` (write/read/space amp definitions, memtable/merge example
  `write_buffer_size=512MB × max 5 = 2.5GB cap`, `Get` scans all immutables):
  `https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide`
- RocksDB `Write Stalls` (stop/slow triggers, `delayed_write_rate`, mitigation:
  flush threads, `max_write_buffer_number`, `write_buffer_size`,
  `min_write_buffer_number_to_merge`):
  `https://github.com/facebook/rocksdb/wiki/Write-Stalls`
- RocksDB `options.h` (`OptimizeForSmallDb` for <1GB, `write_buffer_size`/`max_write_buffer_number` semantics):
  `https://github.com/facebook/rocksdb/blob/main/include/rocksdb/options.h`
- HNSW-vs-DiskANN memory behavior + jemalloc decay profile + block-cache 10% observation
  (secondary — GitHub issue, not docs):
  `https://github.com/surrealdb/surrealdb/issues/7337`
- Block-cache configurability precedent (`SURREAL_ROCKSDB_BLOCK_CACHE_SIZE`, 32MiB example)
  (secondary — GitHub issue): `https://github.com/surrealdb/surrealdb/issues/5541`
