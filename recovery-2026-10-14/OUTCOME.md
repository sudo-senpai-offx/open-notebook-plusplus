# Outcome, after recovery

## Restored

| | |
|---|---|
| File-backed sources | 15 (re-ingested from the PDFs still on disk) |
| Link sources | 20 (decoded from the blob store) |
| Metadata-only | 11 |
| Load-test fixtures among those | 8 |
| **Total sources** | **46** |
| **Total extracted text** | **731,366 chars** |

7 notebooks, 2 notes and 6 source insights were never touched.

## Not restored

- 258 source records existed; 46 exist now. 212 are gone for good.
- 2,754 embeddings. Regenerable, but only once an embedding model is
  reachable; none is running on the PC at present.
- All `reference` edges, so nothing is filed under a notebook. The
  grouping has to be redone in the UI.

## Outstanding

- `Protein purification_compressed.pdf` is registered but has no text:
  it is a scanned PDF and the OCR engine (Docling) is not installed
  here. The file is intact, so this is recoverable once it is.
- Eight `fixture-throttle-*` records from load testing are in the
  library. They were kept rather than discarded because silently
  dropping recovered data is the same class of mistake as destroying
  it. They are safe to delete.

## Prevention

`open_notebook/database/repository.py` now refuses an unscoped table
delete outside a scratch namespace. `DELETE source` against the real
library now raises `UnscopedDeleteError` instead of running. Scoped
deletes - every one the application performs - are unaffected, and
`tests/test_destructive_query_guard.py` pins both halves.
