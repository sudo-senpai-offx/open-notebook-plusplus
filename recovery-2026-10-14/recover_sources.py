"""Recover source records from a SurrealDB blob store.

Context: `DELETE source` was run against the real library by mistake.
SurrealDB compacted the table immediately, so the live store returns
nothing and every write-ahead log is 0 bytes - there is no replay path.
But compaction only rewrites the *index*; the older `.blob` segments
holding record values are still on disk.

These are SurrealDB's own binary encodings. Reverse-engineering them
exactly is not necessary to recover the data, and guessing wrong on the
container tags is how a parser silently returns nothing. So this takes
the tolerant route:

  * a record header is located by pattern (`*<ns> *<ns> *source *` + id);
  * within that record's byte window, each wanted field is located by
    its length-prefixed *name* (`<len><name>`) rather than by walking
    the tag grammar;
  * the value is then read a few bytes further on, trying both string
    encodings.

A record is emitted if any field was recovered. Partial recovery beats
none, and every recovered field is verifiable against the surviving
notebook names and upload filenames.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

HEADER = re.compile(
    rb"\*[a-z_0-9]+\x00\*[a-z_0-9]+\x00\*source\x00\*\x00\x00\x00\x01([a-z0-9]{10,32})\x00"
)

WANTED = (
    "title",
    "topics",
    "full_text",
    "created",
    "updated",
    "asset",
    "file_path",
    "url",
    "sha256",
    "filename",
    "size",
)

# Field names are stored as <len><name>; build the needle for each.
NEEDLES = {n: bytes([len(n)]) + n.encode() for n in WANTED}

# Marks the start of a string *value*, as opposed to a field name.
VALUE_TAG = b"\x02\x04\x01"

# A record's fields can span blob segments, so allow a generous window,
# but cap it so a bogus length cannot run away to the end of the file.
WINDOW = 24 * 1024 * 1024


def _leb128(buf: bytes, i: int) -> Tuple[Optional[int], int]:
    result = shift = 0
    for _ in range(10):
        if i >= len(buf):
            return None, i
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
    return None, i


def _read_string(buf: bytes, i: int) -> Tuple[Optional[str], int]:
    """Read a length-prefixed string at *i*, or (None, i) if not one."""
    if i >= len(buf):
        return None, i
    lead = buf[i]
    if lead == 0xFB:
        length, j = _leb128(buf, i + 1)
        if length is None or length > len(buf) or j + length > len(buf):
            return None, i
        return buf[j : j + length].decode("utf-8", "replace"), j + length
    if 0 < lead < 0x80 and i + 1 + lead <= len(buf):
        return buf[i + 1 : i + 1 + lead].decode("utf-8", "replace"), i + 1 + lead
    return None, i


def _value_after(buf: bytes, pos: int, limit: int) -> Optional[str]:
    """Read the value that follows a field name at *pos*.

    Decoding the container tags empirically: a string value is always
    introduced by ``02 04 01``, followed by either a one-byte length or
    ``fb`` plus a LEB128 length. Field *names* use a different tag
    (``02 09 01``), which is what makes the two distinguishable.

    Anchoring on that marker rather than guessing a fixed offset is what
    makes this correct - a fixed offset silently returned the *next*
    field's name as this field's value.
    """
    tag = buf.find(VALUE_TAG, pos, min(limit, pos + 24))
    if tag == -1:
        return None
    value, _ = _read_string(buf, tag + len(VALUE_TAG))
    if value is None:
        return None
    printable = sum(1 for c in value[:200] if c.isprintable() or c in "\n\t")
    if value and printable / min(len(value), 200) >= 0.85:
        return value
    return None


def extract(buf: bytes, start: int, limit: int) -> Dict[str, Any]:
    """Collect every wanted field visible in one record's window."""
    out: Dict[str, Any] = {}
    window = buf[start:limit]

    for name, needle in NEEDLES.items():
        at = window.find(needle)
        while at != -1:
            value = _value_after(window, at + len(needle), len(window))
            if value is not None:
                out[name] = value
                break
            at = window.find(needle, at + 1)

    # `asset` is a nested map, so its scalar fields may sit outside the
    # value we read for the map itself. Those are already picked up by
    # the same search, since the needle scan covers the whole window.
    return out


def iter_sources(data: bytes) -> Iterator[Dict[str, Any]]:
    for match in HEADER.finditer(data):
        rid = match.group(1).decode()
        start = match.end()
        fields = extract(data, start, min(len(data), start + WINDOW))
        if not fields:
            continue
        yield {
            "id": f"source:{rid}",
            "title": fields.get("title"),
            "full_text": fields.get("full_text"),
            "file_path": fields.get("file_path"),
            "url": fields.get("url"),
            "sha256": fields.get("sha256"),
            "filename": fields.get("filename"),
            "topics": fields.get("topics"),
        }


def _score(rec: Dict[str, Any]) -> int:
    return sum(len(v) for k, v in rec.items() if k != "id" and isinstance(v, str))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: recover_sources.py <dir> [out.json]", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    best: Dict[str, Dict[str, Any]] = {}
    for blob in sorted(src.glob("*.blob")):
        data = blob.read_bytes()
        fresh = 0
        for rec in iter_sources(data):
            rid = rec["id"]
            prev = best.get(rid)
            if prev is None or _score(rec) > _score(prev):
                best[rid] = rec
                fresh += 1
        print(
            f"  {blob.name:<14} {len(data) / 1e6:6.1f}MB -> {fresh} improved",
            file=sys.stderr,
        )

    records = sorted(best.values(), key=lambda r: r["id"])
    print(f"\n  distinct source records : {len(records)}")
    print(f"  with a title            : {sum(1 for r in records if r.get('title'))}")
    print(
        f"  with full_text          : {sum(1 for r in records if r.get('full_text'))}"
    )
    print(
        f"  with a file_path        : {sum(1 for r in records if r.get('file_path'))}"
    )
    print(f"  with a url              : {sum(1 for r in records if r.get('url'))}")
    if out_path:
        out_path.write_text(json.dumps(records, indent=1, ensure_ascii=False))
        print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
