# Phase 7: Exhaustive Testing & Refinement Plan

## Context
Week 6 (Days 1-5) is complete and pushed to `plusplus/main` through commit `389fa5ec`.
The universal source pre-/post-processor (7-stage pipeline) is built.
Now entering exhaustive testing, refinement, cumulative improvement, ON++ integration,
research/auditing/webfetching, and novel question-driven clarification.

## Testing Gaps Identified

### Backend - source.py 7-stage pipeline (untested code paths)

| Component | Status | Gap |
|-----------|--------|-----|
| `ingest_stage` | ❌ No dedicated tests | Validation, source load, command reference |
| `normalize_stage` | ❌ No dedicated tests | Empty content, scanned PDF, media failure messages |
| `chunk_stage` | ❌ No dedicated tests | Empty text, timeout, checkpoint, sub-minimum chunks |
| `embed_stage` | ⚠️ Partial (e2e only) | Timeout path, empty chunks, skipped when embed=False |
| `index_stage` | ⚠️ Partial (e2e only) | Status persistence, warnings/timed_out_pages, chunk insert, transformations |
| `compensate_*` handlers | ⚠️ Partial (via e2e) | Individual unit tests for each compensator |
| `route_stage` (url/html/text/spreadsheet/csv/image) | ⚠️ Partial (video only) | All non-PDF/audio/video routes |
| `extract_stage` (csv/spreadsheet/image/document) | ⚠️ Partial | Capability gating, fallback to content-core |
| `content_process` edge cases | ❌ No dedicated tests | Empty content, scanned PDF, media without STT |
| `_extract_with_text_engine` | ❌ No dedicated tests | STT fallback, OCR augmentation, media refusal |

### Preprocess tools

| Tool | Status | Gap |
|------|--------|-----|
| `pdf.py` | ⚠️ Partial (via e2e mock) | Empty file, corrupted PDF, page_count=0, language flag |
| `tesseract.py` | ⚠️ Partial | Low confidence, timed-out pages, no OCR binary |
| `ssconvert.py` | ⚠️ Partial | Truncated sheets, missing binary |
| `stt.py` | ⚠️ Partial | Segment timeout, detected language, resume from checkpoint |
| `pandoc.py` | ✅ Tested | Base64 strip, mock subprocess |
| `trafilatura.py` | ✅ Tested | Clean markdown, SPA flag, empty HTML |

### Frontend (untested)

| Component | Status | Gap |
|-----------|--------|-----|
| `SourceStatusBanner` | ❌ No tests | Status badges, timed_out_pages formatter, warning mappings, STT language selector, retry button |
| `QueueBacklogPanel` | ❌ No tests | Queue depth, batch mode, visibility-aware polling |
| `CapabilitiesCard` | ❌ No tests | Capability status rendering |
| `retrySource` API client | ❌ No tests | Language override, retry endpoint |

## ON++ Integration Points

| Integration | Status | Verification Needed |
|-------------|--------|-------------------|
| `retry_source_processing` endpoint | ✅ Built | Wired to `retrySource()` client, language override via PUT |
| `Source.status` / `warnings` / `timed_out_pages` | ✅ Built | Persisted by `index_stage`, rendered by UI |
| Scheduler dispatcher | ✅ Built | Ollama blocked → deferred lane, batch mode unloading |
| Residency (5-min chat idle) | ✅ Built | Heavy job held while chat active, unload/reload gating |
| `SourceUpdate` model | ⚠️ Partial | `language` field not in `SourceUpdate` (only title/topics) |

## Research Targets

- Novel source types (social media, email, chat transcripts, web archives)
- OCR/STT best practices for low-resource languages (ms, ta)
- Content extraction engine comparison (content-core vs docling vs trafilatura)
- Checkpoint/resume patterns for long-running extraction jobs

## Novel Questions for the User

(To be asked - see conversation)
