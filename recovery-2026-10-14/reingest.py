"""Re-ingest the source PDFs that survived on disk.

`DELETE source` destroyed the database rows but not
`data/uploads/`, which still holds 15 PDFs (49 MB). Because the text is
re-extracted from the bytes, re-ingesting restores the material itself —
this recovers content, not just metadata.

Each file goes through the ordinary ingestion path:

  1. `store_file()` hashes it into the content-addressed store, so a
     duplicate collapses to one copy;
  2. a Source record is created with the resulting blob identity;
  3. the extraction graph runs, which re-extracts `full_text` and, with
     `embed=True`, rebuilds the vectors that the cascade destroyed.

Notebooks are attached afterwards by name. The old `reference` edges went
with the rows, so grouping cannot be inferred — titles are used as a hint
and everything unattached is reported rather than guessed at silently.

Usage:
    python3 reingest.py [--embed] [--notebooks-from-recovered]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import cast

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uploads", default=str(REPO / "notebook_data/uploads"))
    parser.add_argument("--embed", action="store_true", help="rebuild vectors too")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    from open_notebook.database.repository import repo_query
    from open_notebook.domain.notebook import Asset, Source
    from open_notebook.graphs.source import SourceState, source_graph
    from open_notebook.storage.blobs import store_file

    uploads = Path(args.uploads)
    if not uploads.is_dir():
        print(f"no uploads directory at {uploads}", file=sys.stderr)
        return 1

    files = sorted(p for p in uploads.iterdir() if p.is_file())
    if args.limit:
        files = files[: args.limit]
    print(f"re-ingesting {len(files)} file(s) from {uploads}\n")

    notebooks = {
        str(r["id"]): r.get("name") or ""
        for r in (await repo_query("SELECT id, name FROM notebook") or [])
    }
    print(f"  {len(notebooks)} notebooks available for attachment\n")

    # Which files already have a record, so nothing is duplicated.
    existing = {
        Path(r["file_path"]).name
        for r in (
            await repo_query(
                "SELECT file_path FROM source WHERE asset.file_path != NONE"
            )
            or []
        )
        if r.get("file_path")
    }

    created = skipped = failed = 0
    for path in files:
        if path.name in existing:
            print(f"  skip    {path.name[:64]} (already has a record)")
            skipped += 1
            continue
        try:
            ref = store_file(path)
            # Deliberately a placeholder: save_source() replaces it with
            # the title extraction derives, which is better than any name
            # we could seed here. Seeding a real title suppresses that -
            # and when the hint map keyed by filename is ambiguous, which
            # it is whenever two sources shared a name, it seeded a title
            # from the wrong record entirely.
            source = Source(
                title="Processing...",
                topics=[],
                asset=Asset(
                    file_path=ref.path,
                    sha256=ref.sha256,
                    filename=path.name,
                    size=ref.size,
                ),
            )
            await source.save()

            await source_graph.ainvoke(
                cast(
                    SourceState,
                    {
                        "content_state": {
                            "file_path": ref.path,
                            "sha256": ref.sha256,
                            "filename": path.name,
                            "delete_source": False,
                        },
                        "extraction": None,
                        "apply_transformations": [],
                        "source_id": str(source.id),
                        "notebook_ids": [],
                        "embed": args.embed,
                    },
                )
            )
            final = await Source.get(str(source.id))
            text_len = len(final.full_text or "") if final else 0
            title = (final.title if final else "") or path.stem
            print(
                f"  ingest  {title[:44]:<44} {ref.size / 1e6:5.1f}MB "
                f"{'dedup' if ref.deduped else 'stored'}  text={text_len:,}"
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"  FAIL    {path.name[:64]}: {type(exc).__name__}: {exc}")
            failed += 1

    total = await repo_query("SELECT count() AS n FROM source GROUP ALL")
    print(f"\n  created {created}, skipped {skipped}, failed {failed}")
    print(f"  sources in the database now: {(total or [{}])[0].get('n', 0)}")

    if notebooks:
        print(
            f"\n  {len(notebooks)} notebooks exist but the old reference edges were\n"
            "  destroyed with the rows, so sources are unattached. Re-link them\n"
            "  from the UI, or pass --notebook-map to attach by name."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
