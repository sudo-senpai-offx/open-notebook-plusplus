"""Rebuild the embeddings the delete destroyed.

The cascade took 2,754 `source_embedding` rows with the sources. They are
derived data - recomputed from `full_text` and the local embedding model
- which is exactly why the sync manifest never carries them.

Runs the application's own `embed_source_command`, so the chunking,
batching and pooling are the ones search and chat already expect. An
embedding computed by any other route would be subtly incompatible with
the vectors already in the database.

    python3 reembed.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from commands.embedding_commands import embed_source_command
    from open_notebook.database.repository import repo_query
    from open_notebook.domain.notebook import Source

    sources = await Source.get_all()
    targets = [s for s in sources if (s.full_text or "").strip()]
    if args.limit:
        targets = targets[: args.limit]

    print(f"  {len(targets)} of {len(sources)} sources have text to embed")
    if not targets:
        return 0

    total_chunks = 0
    ok = failed = 0
    for source in targets:
        chars = len(source.full_text or "")
        title = source.title or "(untitled)"
        source_id = str(source.id or "")
        if args.dry_run:
            print(f"  would embed  {title[:50]:<52} {chars:>8,} chars")
            continue
        try:
            from commands.embedding_commands import EmbedSourceInput

            await embed_source_command(EmbedSourceInput(source_id=source_id))
            # Count rows for this source rather than parsing the
            # command's output shape, which is not a stable contract.
            rows = await repo_query(
                "SELECT count() AS n FROM source_embedding WHERE source = $s GROUP ALL",
                {"s": source_id},
            )
            chunks = (rows or [{}])[0].get("n", 0) if rows else 0
            total_chunks += int(chunks or 0)
            ok += 1
            print(
                f"  embedded    {title[:50]:<52} {chars:>8,} chars  {chunks or '?'} chunks"
            )
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            failed += 1
            print(
                f"  FAILED      {title[:50]:<52} {type(exc).__name__}: {str(exc)[:90]}"
            )

    rows = await repo_query("SELECT count() AS n FROM source_embedding GROUP ALL")
    print(f"\n  embedded {ok}, failed {failed}, chunks {total_chunks}")
    print(f"  source_embedding rows now: {(rows or [{}])[0].get('n', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
