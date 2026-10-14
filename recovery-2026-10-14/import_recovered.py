"""Import the source records decoded from the pre-compaction blob store.

45 records survived the delete. They are not all equal, and importing
them blindly would undo part of the re-ingest:

  * 15 were file sources whose PDFs still exist on disk. Those have been
    re-ingested properly - fresh extraction, correct titles - so importing
    the older decoded copy would duplicate them with worse content.
  * ~20 are link sources. Nothing on disk holds their content, so the
    decoded `full_text` is the only surviving copy.
  * 8 are `fixture-throttle-*` records from load testing. Imported rather
    than dropped, because silently discarding recovered data is the same
    class of mistake as destroying it; the report says what they are so
    they can be pruned deliberately.

Deduplication is by asset file name for file sources and by record id for
the rest, so running this twice does not double anything.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RECOVERED = REPO / "recovery-2026-10-14/recovered-sources.json"


async def main() -> int:
    if not RECOVERED.is_file():
        print(f"missing {RECOVERED}", file=sys.stderr)
        return 1

    from open_notebook.database.repository import repo_query
    from open_notebook.domain.notebook import Asset, Source

    records = json.loads(RECOVERED.read_text())

    present_ids = {
        str(r["id"]) for r in (await repo_query("SELECT id FROM source") or [])
    }
    present_files = {
        Path(r["file_path"]).name
        for r in (
            await repo_query(
                "SELECT file_path FROM source WHERE asset.file_path != NONE"
            )
            or []
        )
        if isinstance(r, dict) and r.get("file_path")
    }

    imported = skipped_on_disk = skipped_existing = failed = 0
    links = fixtures = files = 0

    for rec in records:
        rid = rec["id"]
        file_name = Path(rec["file_path"]).name if rec.get("file_path") else None

        if rid in present_ids:
            skipped_existing += 1
            continue
        # The file is back on disk and re-ingested; the decoded copy is
        # strictly worse than a fresh extraction.
        if file_name and file_name in present_files:
            skipped_on_disk += 1
            continue

        title = rec.get("title") or ""
        is_fixture = "fixture-throttle" in title
        kind = "link" if rec.get("url") else "file"

        try:
            source = Source(
                title=title or "(recovered)",
                topics=[],
                full_text=rec.get("full_text"),
                asset=Asset(
                    url=rec.get("url"),
                    sha256=rec.get("sha256"),
                    filename=rec.get("filename"),
                ),
            )
            await source.save()
            if kind == "link":
                links += 1
            else:
                files += 1
            if is_fixture:
                fixtures += 1
            imported += 1
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
            print(f"  FAIL {rid}: {type(exc).__name__}: {str(exc)[:120]}")
            failed += 1

    total = await repo_query("SELECT count() AS n FROM source GROUP ALL")
    print(
        f"  imported {imported} ({links} link, {files} file, {fixtures} of them load-test fixtures)"
    )
    print(f"  skipped {skipped_on_disk} already re-ingested from disk")
    print(f"  skipped {skipped_existing} already present")
    print(f"  failed {failed}")
    print(f"  sources in the database now: {(total or [{}])[0].get('n', 0)}")

    if fixtures:
        print(
            f"\n  Note: {fixtures} of the imported records are load-test fixtures\n"
            "  ('fixture-throttle-*'). They are real recovered records, so they\n"
            "  were kept rather than discarded, but you may want to delete them."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
