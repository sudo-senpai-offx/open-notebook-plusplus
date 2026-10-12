# Low-end embeddings research (2 GB / 2-thread CPU, Ollama)

Scope: cheaper embedding models that run on a weak CPU, int8/binary quantization
support in common vector paths, chunk-size vs retrieval-quality guidance, hybrid
(dense+sparse) CPU cost. Read-only research; no code changed.

Local baseline this must fit: app 2 GiB/2c envelope, `EMBED_BATCH_SIZE=8` in
`docker-compose.yml`, chunk default 400 tokens with 15% overlap, long text
mean-pooled (`docs/7-DEVELOPMENT/content-processing.md`); embed 1 chunk = 5.79 s,
14 chunks = 16.5 s on local Ollama; worker task peak ~234 MB
(`docs/7-DEVELOPMENT/decisions/ADR-009-low-end-x86-optimization.md`).
Note: compose sets `EMBED_BATCH_SIZE=8` but `open_notebook/utils/embedding.py`
reads `OPEN_NOTEBOOK_EMBEDDING_BATCH_SIZE` (default 50) — verify which var the
worker actually honors before tuning batch.

## 1. Model ladder (smallest → largest)

All params/dims/context/MTEB below are vendor- or HF-card-reported, not measured here.

| # | Model (Ollama tag where available) | Params | Dim / ctx | Pull size (Ollama) | Retrieval quality note |
|---|---|---|---|---|---|
| 1 | `all-minilm` (`:22m`, `:33m`) | 22.7M ([HF card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)) | 384 dim / truncates >256 word pieces ([HF card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)) | 46 MB `:22m`, 67 MB `:33m` ([Ollama library](https://ollama.com/library/all-minilm)) | Cheapest; MTEB-retrieval ~41.7 float32 ([HF quantization study](https://huggingface.co/blog/embedding-quantization)); Snowflake's table has it at 41.95 vs 50.15 for their xs rebuild on the same base ([arctic-xs card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)) |
| 2 | `snowflake-arctic-embed:22m` (xs) | 22M ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)) | 384 dim / 512 | Ollama lists 5 sizes (`:22m/:33m/:110m/:137m/:335m`, default `:335m`) but per-variant byte sizes are not published on the listing page ([Ollama library](https://ollama.com/library/snowflake-arctic-embed:m)) | MTEB retrieval (NDCG@10) 50.15 — ~8 pts above stock MiniLM on the same 22M base ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)). Best quality-per-MB in the ladder |
| 3 | `snowflake-arctic-embed:33m` (s) / BGE-small-en-v1.5 (GGUF, no Ollama tag) | 33M / 33.4M ([arctic-xs card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs), [BGE-small card](https://huggingface.co/BAAI/bge-small-en-v1.5)) | 384 dim / 512 both ([BGE-small card](https://huggingface.co/BAAI/bge-small-en-v1.5)) | — (Ollama has no `bge-small` tag; only `bge-m3` below) | arctic-s 51.98 vs bge-small-en-v1.5 51.68 vs e5-small-v2 49.04 ([arctic-xs card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)); BGE-small MTEB avg 62.17, retrieval 51.68 ([BGE-small card](https://huggingface.co/BAAI/bge-small-en-v1.5)) |
| 4 | `snowflake-arctic-embed:110m` (m) | 110M ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)) | 768 dim / 512 | see row 2 (Ollama sizes unlisted) | Retrieval 54.90 — beats bge-base-en-v1.5 (53.25) and nomic-1.5 (53.25/53.01) on Snowflake's table ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)). Strong mid-ladder pick if 512 ctx suffices |
| 5 | `nomic-embed-text` (current default, v1.5) | 0.137B / 12 layers / 768 hidden ([inference bench](https://inferencebench.io/models/nomic/nomic-embed-text-v1.5)); HF lists 0.1B params ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)) | 768 dim / 8192 ctx ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)) | 274 MB ([Ollama library search](https://ollama.com/search?c=embedding) via `nomic-embed-text:v1.5`) | MTEB 62.28 @768 → 61.96 @512 → 61.04 @256 → 59.34 @128 → 56.10 @64 ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5), [Nomic docs](https://docs.nomic.ai/atlas/embeddings-and-retrieval/text-embedding)). Requires `search_document:`/`search_query:` prefixes ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)). BF16 weights ~0.3 GB, FP8/INT4 ~0.1 GB + ~0.05 GB activations ([inference bench](https://inferencebench.io/models/nomic/nomic-embed-text-v1.5)); min RAM 0.1 GB / rec 0.3 GB F16 ([VRAM guide](https://willitrunai.com/models/nomic-embed-text-v1.5)) |
| 6 | `snowflake-arctic-embed:137m` (m-long) | 137M ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)) | 768 dim / 2048 native, 8192 with RPE ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)) | see row 2 | Retrieval 54.83 — the long-context alternative to nomic at the same weight class ([HF card](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs)) |
| 7 | `mxbai-embed-large` (`:335m`) | 335M (tag name; BERT-large class) | 1024 dim(?) / 512 ctx | 670 MB ([Ollama library](https://ollama.com/library/mxbai-embed-large)) | SOTA-for-size at release, "outperforms `text-embedding-3-large`, matches models 20x its size" ([Ollama library](https://ollama.com/library/mxbai-embed-large)); MTEB-retrieval 54.39 float32 ([HF study](https://huggingface.co/blog/embedding-quantization)). Quality ceiling for the box; 2.4x the pull of nomic, 512 ctx only |
| 8 | `bge-m3` (`:567m`) — NOT recommended for 2 GB | 567M ([Ollama library](https://ollama.com/library/bge-m3)) | multi-function dense+sparse+ColBERT, 8192 tokens ([Ollama library](https://ollama.com/library/bge-m3)) | 1.2 GB ([Ollama library](https://ollama.com/library/bge-m3)) | Most capable, but 4.4x nomic's pull and the heaviest inference; skip on this envelope (consistent with ADR-009's headroom math) |

Takeaway: keep `nomic-embed-text-v1.5` as default (only long-context + resizable
option at ~274 MB). Downgrade path on memory pressure: arctic-m-long (137m) →
arctic-m (110m) → arctic-s (33m) → arctic-xs (22m). `all-minilm` is only for the
absolute floor — arctic-xs dominates it at equal size. `mxbai-embed-large` is the
quality-upgrade experiment; `bge-m3` stays out.

## 2. Quantization: int8 / binary / Matryoshka

### 2a. What the numbers say (primary: HF + Qdrant + vendors)

- int8 scalar = exactly 4x memory/index saving; binary = exactly 32x; retrieval
  speedups measured on CPU exact search: int8 mean 3.66x (2.99–4.8x), binary mean
  24.76x (15–45.8x) ([HF embedding-quantization](https://huggingface.co/blog/embedding-quantization)).
- Quality retention is model-dependent, with rescoring (retrieve 4x candidates in
  quantized space, rescore with float32 query): mxbai-large int8 97%, binary
  96.45% (92.5% without rescore); Cohere-embed int8 100%, binary 94.6%;
  nomic-1.5 binary 87.7%; all-MiniLM int8 90.79%, binary 93.79%
  ([HF study table](https://huggingface.co/blog/embedding-quantization)). int8 with
  rescore multiplier 4–5 reaches ~99% on mxbai-large ([same](https://huggingface.co/blog/embedding-quantization)).
- Caveat: quantization does not work equally everywhere — e5-base-v2 collapses
  (dimension collapse), calibration data choice matters for int8 buckets
  ([HF study](https://huggingface.co/blog/embedding-quantization)).
- Qdrant natively supports scalar int8 (4x, "usually <1% error, less for
  high-dim"), binary 1-bit (32x, "up to 40x speedup", needs high-dim centered
  distributions + rescoring), 2-bit (16x) / 1.5-bit (~21x) for small vectors,
  TurboQuant bits4 (8x, "similar recall to scalar at 2x compression"), and PQ up
  to 64x ([Qdrant quantization](https://qdrant.tech/documentation/guides/quantization/)).
- pgvector 0.7.0 added `halfvec` (2-byte floats, index up to 4000 dims) and `bit`
  (binary, index up to 64000 dims) ([pgvector README](https://github.com/pgvector/pgvector),
  [jkatz guide](https://jkatz.github.io/post/postgres/pgvector-scalar-binary-quantization/),
  [Supabase release notes](https://supabase.com/blog/pgvector-0-7-0)).
- Vendors ship the same recipe: Voyage `output_dtype` int8/uint8 (4x) and
  binary/ubinary (32x, bit-packed int8, 1/8th length) with Matryoshka truncation
  composed on top ([Voyage docs](https://docs.voyageai.com/docs/flexible-dimensions-and-quantization));
  Cohere Embed natively supports int8 + binary embedding types
  ([Cohere blog](https://cohere.com/blog/int8-binary-embeddings),
  [HF companion summary](https://huggingface.co/blog/embedding-quantization)).
- Matryoshka (MRL) is orthogonal to quantization: truncate nomic-1.5 768→256 for
  ~3x storage at 62.28→61.04 MTEB (~2% loss); 768→128 still 59.34
  ([HF card table](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)); HF
  estimates stacking MRL + binary/quantization at up to 32x for ~3% or 256x for
  ~10% loss ([HF study](https://huggingface.co/blog/embedding-quantization)).
  Truncation protocol is layer_norm → slice → re-normalize
  ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)).

### 2b. What this means for SurrealDB (the actual vector path here)

- SurrealDB HNSW accepts `TYPE F64 | F32 | I64 | I32 | I16` (default `F32`); there
  is **no native binary/bit or int8-quantized vector path** on HNSW
  ([DEFINE INDEX docs](https://surrealdb.com/docs/reference/query-language/statements/define/indexes)).
- DISKANN accepts only `F32 | F16 | I8 | U8`, vectors stored/searched at full
  precision with no product quantization — the only size lever is `TYPE`
  ([same docs](https://surrealdb.com/docs/reference/query-language/statements/define/indexes)).
- HNSW graph is memory-resident; DISKANN is key-value-backed/paged. Both caches
  share one 256 MiB default budget each (`SURREAL_HNSW_CACHE_SIZE`,
  `SURREAL_DISKANN_CACHE_SIZE`) ([same docs](https://surrealdb.com/docs/reference/query-language/statements/define/indexes)).
- Implication: int8/binary index savings from Qdrant/pgvector do **not** transfer
  to this stack without app-level work (quantize at write, rescore at query, as
  in the HF recommended pipeline: binary search top-40 → int8 rescore → top-10,
  200 GB → 5.2 GB RAM + 52 GB disk on 41M Wikipedia vectors
  ([HF study](https://huggingface.co/blog/embedding-quantization))). The
  zero-code storage win available today is Matryoshka truncation (768→256),
  which needs only an embedding-dimension change, not index support.

## 3. Chunk size vs retrieval quality

- Start fixed-size, then iterate: Pinecone recommends fixed-size chunking first,
  exploring 128/256 for granularity vs 512/1024 for context; chunks must fit the
  embedder's context window and "make sense without surrounding context"
  ([Pinecone chunking strategies](https://www.pinecone.io/learn/chunking-strategies/)).
- LlamaIndex's Uber-10K experiment (chunk sizes 128–2048, faithfulness +
  relevancy judges): small chunks (128) risk the answer missing top-k;
  512 "likely encompasses all necessary information"; faithfulness and relevancy
  both peaked at 1024 with only a minor response-time uptick
  ([LlamaIndex blog](https://www.llamaindex.ai/blog/evaluating-the-ideal-chunk-size-for-a-rag-system-using-llamaindex-6207e5d3fec5)).
- Current 400-token default is principled for 512-ctx BERT-family embedders: ~20%
  under the 512 ceiling as buffer for tokenizer mismatch, splitter overshoot,
  special tokens; raise to ~1500 only for large-window embedders
  (`docs/7-DEVELOPMENT/content-processing.md`). Matryoshka/8K nomic removes the
  truncation risk but not the dilution risk (large chunks dilute per-sentence
  signal — [Pinecone](https://www.pinecone.io/learn/chunking-strategies/)).
- Recommendation for this box: keep 400/15% as default; A/B 256 vs 400 vs 800 on
  the fixture corpus (9 chunks, `notebook:1n4cb4qr744izp5xdo26` per ADR-009 A2 #6)
  before touching 1500. Prefer chunk-expansion at query time (retrieve small,
  expand to neighbors) over globally larger chunks
  ([Pinecone](https://www.pinecone.io/learn/chunking-strategies/)) — it keeps
  embed cost (5.79 s/chunk local) linear while recovering context.

## 4. Hybrid dense+sparse CPU cost

- Shape: dense prefetch + BM25 sparse prefetch, fused with RRF; BM25 weights come
  from TF/IDF/doc-length statistics — no model inference at index or query
  ([Qdrant hybrid search](https://qdrant.tech/documentation/search-tuning/hybrid-search/)).
- Measured cost (single container, one request at a time, all-MiniLM-L6-v2 dense +
  BM25): second prefetch adds +0.60 to +1.47 ms median query latency; plus one
  sparse vector per point, a second index, and a re-upsert pass over existing data
  ([Qdrant hybrid search](https://qdrant.tech/documentation/search-tuning/hybrid-search/)).
- Gain is real but not automatic: fusion beat the better single retriever on 4/5
  datasets (+0.0156 to +0.0311 nDCG@10) and lost on DBpedia-entity (−0.0039)
  ([Qdrant tuning guide](https://qdrant.tech/documentation/search-tuning/how-to-tune-hybrid-search/index.md)).
  Rule: keep sparse only if the measured nDCG@10 gain justifies the indexing +
  latency cost ([same](https://qdrant.tech/documentation/search-tuning/how-to-tune-hybrid-search/index.md)).
- Learned sparse costs far more: SPLADE_PP_en ONNX is 0.532 GB vs BM25's 0.01 GB
  mock + BM42-MiniLM 0.09 GB; SPLADE needs transformer inference per doc and per
  query, BM25 needs none ([Qdrant sparse models](https://github.com/qdrant/landing_page/blob/master/qdrant-landing/content/articles/modern-sparse-neural-retrieval.md)).
  Start with BM25, measure a learned model against it before adopting
  ([Qdrant hybrid search](https://qdrant.tech/documentation/search-tuning/hybrid-search/)).
- For this stack: SurrealDB already has `FULLTEXT ANALYZER … BM25` full-text
  indexes ([DEFINE INDEX docs](https://surrealdb.com/docs/reference/query-language/statements/define/indexes)),
  so the sparse side exists — but fusion would be app-level (vector KNN +
  full-text + RRF in the API), with no native hybrid operator. That keeps the
  storage/indexing cost (second index, re-embed pass) while the query cost stays
  small at this corpus size (fixture: 9 chunks). Defer until dense-only recall is
  measured inadequate on labeled queries.

## 5. Batch / chunk recommendations for 2 GB, 2-thread CPU

| Knob | Recommendation | Basis |
|---|---|---|
| Model | Keep `nomic-embed-text`; fallback arctic-xs/s on OOM | §1 ladder; nomic 274 MB vs mxbai 670 MB vs bge-m3 1.2 GB ([Ollama](https://ollama.com/library/mxbai-embed-large), [Ollama](https://ollama.com/library/bge-m3)) |
| Chunk | 400 tokens / 15% overlap default; trial 256 and 800 | §3; 400-buffer rationale in content-processing.md; 128/256 vs 512/1024 sweep guidance ([Pinecone](https://www.pinecone.io/learn/chunking-strategies/)) |
| Batch | 4–8 texts/inflight on 2 threads; resolve `EMBED_BATCH_SIZE` vs `OPEN_NOTEBOOK_EMBEDDING_BATCH_SIZE` first | Compose uses 8; Infinity's nomic CPU example uses `--batch-size 8` ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)); Qdrant tutorial batches 25 with hosted inference (not a CPU-local number) |
| Dim | Trial nomic Matryoshka 768→256 (normalize→slice→normalize) | −1.24 MTEB pts for 3x vector storage ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5), [Nomic docs](https://docs.nomic.ai/atlas/embeddings-and-retrieval/text-embedding)); only storage win SurrealDB supports (§2b) |
| Hybrid | Dense-only until labeled eval says otherwise | +0.6–1.5 ms/query is cheap, but second index + re-upsert + app fusion is not free ([Qdrant](https://qdrant.tech/documentation/search-tuning/hybrid-search/)) |

## 6. What to measure (before changing anything)

1. Embed throughput: s/chunk and s/14-chunk task at batch 4 vs 8 (baseline:
   5.79 s / 16.5 s per ADR-009) + worker RSS peak (baseline ~234 MB).
2. Model swap: peak Ollama RSS for nomic vs arctic-xs/s/m on the 12-chunk probe;
   record cold-load latency (worker spawn→ready 12.7–13.1 s baseline).
3. Chunk A/B on fixture `notebook:1n4cb4qr744izp5xdo26`: recall@k / nDCG@10 at
   256 vs 400 vs 800 with fixed top-k (mirrors the LlamaIndex faithfulness /
   relevancy protocol ([blog](https://www.llamaindex.ai/blog/evaluating-the-ideal-chunk-size-for-a-rag-system-using-llamaindex-6207e5d3fec5))).
4. Matryoshka 768→256→128: index bytes + recall@k delta (expect ~−1.2 / ~−3 pts
   per the vendor table ([HF card](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5))).
5. Hybrid gate: labeled query set, dense vs full-text-BM25 vs app-fused RRF
   nDCG@10 + p50/p95 latency — adopt only on a positive delta
   ([Qdrant tuning guide](https://qdrant.tech/documentation/search-tuning/how-to-tune-hybrid-search/index.md)).
