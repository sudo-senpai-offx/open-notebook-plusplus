# Recovery, 2026-10-14

`DELETE source` was run against the `open_notebook` namespace without a
namespace guard or a WHERE clause. SurrealDB compacted the table
immediately.

## What survived in the database

- 7 notebooks
- 2 notes
- 6 source insights

## What was lost

- 258 source records
- 2,754 source_embedding rows (cascaded via the record link)

## What was recovered

45 source records decoded from the pre-compaction `.blob` segments,
which compaction had not yet reclaimed. Of those:

- 8 are `fixture-throttle-*` load-test records
- ~20 are NFL/tennis news links from site-crawling tests
- ~11 are genuine course PDFs (Bioinformatic tools, CRISPR, Genomic
  library Construction, INTRO TO MOLECULAR BIOLOGY, Production of
  protein from cloned genes, Prokaryotic Gene Regulation, and others)
- 135 KB of extracted text in total

213 records are not present in any surviving segment and are not
recoverable from this store.

## The originals are intact

`notebook_data/uploads` still holds all 15 source PDFs (49 MB).
Re-ingesting them restores the full content, because the text is
re-extracted from the bytes. Five of the fifteen have no surviving
record but the file is present, so nothing was actually lost for those.

## Re-running the decoder

    python3 recover_sources.py <dir-of-blob-files> out.json

The parser is documented inline. It was validated by checking that
recovered `file_path` values match filenames present in `uploads/`.
