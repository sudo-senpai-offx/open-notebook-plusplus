#!/usr/bin/env python3
"""Build multilingual speech fixtures from FLEURS, on the PC.

Downloads the dev split of five locales (~937 MB total), extracts a handful of
clips each with their ground-truth transcripts, and writes a manifest. The
result is pushed to the phone; the phone never downloads the corpus itself.

    python scripts/build-speech-fixtures.py --out notebook_data/speech-fixtures
    python scripts/build-speech-fixtures.py --out DIR --per-locale 5   # more clips
    python scripts/build-speech-fixtures.py --out DIR --languages en_us,ja_jp

Why the PC does this
-------------------
FLEURS ships audio as `{split}.tar.gz`, 163-228 MB per locale. Five locales is
937 MB to test about twenty-five clips. Pulling that onto the phone to validate
STT would cost the device an hour of bandwidth and 1 GB of storage for fixtures
that are, in total, a couple of megabytes. The phone gets the clips; the PC does
the downloading.

Why FLEURS
----------
Real recorded speech with a reference transcript, so transcription accuracy is a
measured number rather than a judgement call. Coverage of the requested set:

    en_us  English
    cmn_hans_cn  Mandarin (Simplified)
    ja_jp  Japanese
    ta_in  Tamil
    ms_my  Malay

Taiwanese Mandarin is not available: FLEURS has mainland Mandarin and Cantonese
(`yue_hant_hk`), and Cantonese is a different language, not a Taiwanese accent.
That gap is recorded in the manifest rather than papered over.

The tarball is only ever extracted for the clips that were asked for — the
members are streamed one at a time and the rest are skipped, so peak extra disk
is one clip.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import struct
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

HF = "https://huggingface.co/datasets/google/fleurs/resolve/main"

# locale -> the label we report. These are FLEURS locale ids, not ISO 639-1:
# Mandarin is cmn_hans_cn, not zh, because FLEURS distinguishes it from yue.
LOCALES = {
    "en_us": {"label": "English", "lang": "en"},
    "cmn_hans_cn": {"label": "Mandarin (Simplified)", "lang": "zh"},
    "ja_jp": {"label": "Japanese", "lang": "ja"},
    "ta_in": {"label": "Tamil", "lang": "ta"},
    "ms_my": {"label": "Malay", "lang": "my"},
}

# Recorded, not assumed. Each is the size the Hugging Face tree API reported;
# a mismatch means the upstream file changed, and a truncated archive would
# otherwise extract to a few clips and look like a corpus problem. The first run
# of this script caught itself here: a size transcribed by hand was wrong by
# 262,304 bytes and the download was discarded rather than half-extracted.
EXPECTED_DEV_TARBALL = {
    "en_us": 171_250_900,
    "cmn_hans_cn": 217_347_747,
    "ja_jp": 179_387_192,
    "ta_in": 238_893_728,
    "ms_my": 175_565_952,
}


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def download(url: str, dest: Path, expect: int | None = None) -> bool:
    """Fetch `url` to `dest`, verifying the byte count when one is known.

    The same discipline as the phone's model fetcher: a truncated download that
    is merely non-empty is worse than no download, because it extracts to a
    partial corpus and the failure surfaces later as missing fixtures.
    """
    have = dest.stat().st_size if dest.exists() else 0
    if expect and have == expect:
        log(f"{dest.name}: {have} bytes, complete")
        return True
    if have:
        log(f"{dest.name}: {have} bytes, wrong size, discarding")
        dest.unlink()

    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"{dest.name}: downloading (~{expect // 2**20 if expect else '?'} MB)")
    try:
        with (
            urllib.request.urlopen(url, timeout=120) as response,
            tmp.open("wb") as out,
        ):
            total = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
    except (urllib.error.URLError, TimeoutError) as exc:
        log(f"{dest.name}: FAILED - {exc}")
        tmp.unlink(missing_ok=True)
        return False

    if expect and total != expect:
        log(f"{dest.name}: got {total} bytes, expected {expect} - discarding")
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    log(f"{dest.name}: {total} bytes, complete")
    return True


def parse_transcripts(text: str) -> dict[str, tuple[str, str]]:
    """filename -> (id, reference transcript).

    Two things about this format that a DictReader gets wrong, both found by
    running it against the real file:

    - **There is no header row.** The first line is already data, so DictReader
      consumes the first clip as column names and every subsequent lookup for
      `id` returns None — silently, yielding zero transcripts.
    - **`id` is not the filename.** A row looks like

          1590<TAB>15158676295442294624.wav<TAB>The main local beer is ...

      so keying on `id` and then matching archive members by stem can never
      match. The archive is named by the *file* column.

    Columns, tab-separated, 7 fields:
      0 id   1 file   2 transcription   3 raw_transcription   4-6 split/locale/...
    `raw_transcription` is the same text before normalisation; `transcription` is
    the scored one, so that is what is used as the reference.
    """
    out: dict[str, tuple[str, str]] = {}
    for row in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(row) < 3:
            continue
        clip_id, filename, transcript = row[0].strip(), row[1].strip(), row[2].strip()
        if not filename or not transcript:
            continue
        out[Path(filename).stem] = (clip_id, transcript)
    return out


def wav_duration(path: Path) -> float:
    """Duration in seconds, read from the WAV header.

    `wave` cannot: FLEURS ships pcm_f32le, which the stdlib reader rejects as an
    unknown format. The header alone is enough, and parsing it by hand avoids
    depending on a decoder for what is only arithmetic.

    The duration goes into the manifest because the suite's per-clip timeout is
    expressed as a multiple of audio length — an absolute timeout either wastes
    time on short clips or truncates long ones, and which one it does is not
    knowable until a clip is genuinely slow.
    """
    with path.open("rb") as handle:
        riff, _size, wave_tag = struct.unpack("<4sI4s", handle.read(12))
        if riff != b"RIFF" or wave_tag != b"WAVE":
            raise ValueError(f"{path.name}: not a RIFF/WAVE file")
        while True:
            head = handle.read(8)
            if len(head) < 8:
                raise ValueError(f"{path.name}: no data chunk")
            chunk_id, chunk_size = struct.unpack("<4sI", head)
            if chunk_id == b"fmt ":
                body = handle.read(chunk_size)
                channels = struct.unpack("<H", body[2:4])[0]
                rate = struct.unpack("<I", body[4:8])[0]
                bits = struct.unpack("<H", body[14:16])[0]
                handle.seek(chunk_size % 2, 1)
            elif chunk_id == b"data":
                return chunk_size / (channels * rate * bits // 8)
            else:
                handle.seek(chunk_size + (chunk_size % 2), 1)


def extract_clips(
    archive: Path,
    wanted: set[str],
    out_dir: Path,
    transcripts: dict[str, tuple[str, str]],
    locale: str,
) -> list[dict]:
    """Pull the wanted members out of the tarball, one at a time.

    Streaming rather than extracting wholesale: the archive is ~200 MB and only
    a few clips are wanted, so peak extra disk is one clip rather than the lot.
    """
    written: list[dict] = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            stem = Path(member.name).stem
            if stem not in wanted or not member.isfile():
                continue
            data = tar.extractfile(member)
            if data is None:
                continue
            clip_id, reference = transcripts.get(stem, ("", ""))
            payload = data.read()
            suffix = Path(member.name).suffix or ".wav"
            target = out_dir / f"{locale}_{stem}{suffix}"
            target.write_bytes(payload)
            try:
                duration = round(wav_duration(target), 3)
            except (ValueError, OSError, struct.error) as exc:
                log(f"{target.name}: cannot read duration ({exc})")
                duration = 0.0
            written.append(
                {
                    "id": clip_id,
                    "stem": stem,
                    "locale": locale,
                    "file": target.name,
                    "bytes": len(payload),
                    "duration_s": duration,
                    "reference": reference,
                }
            )
            if len(written) >= len(wanted):
                break
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--per-locale", type=int, default=3)
    parser.add_argument("--languages", default=",".join(LOCALES))
    parser.add_argument(
        "--cache", type=Path, default=Path("notebook_data/fleurs-cache")
    )
    parser.add_argument(
        "--keep-archive",
        action="store_true",
        help="keep the ~937 MB of tarballs for a later run",
    )
    args = parser.parse_args()

    locales = [x.strip() for x in args.languages.split(",") if x.strip()]
    unknown = [x for x in locales if x not in LOCALES]
    if unknown:
        print(f"unknown locale(s): {unknown}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    args.cache.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    failures: list[str] = []

    for locale in locales:
        print(f"\n==> {locale} ({LOCALES[locale]['label']})")
        tsv_path = args.cache / f"{locale}_dev.tsv"
        tar_path = args.cache / f"{locale}_dev.tar.gz"

        if not tsv_path.exists() or tsv_path.stat().st_size == 0:
            try:
                with urllib.request.urlopen(
                    f"{HF}/data/{locale}/dev.tsv", timeout=60
                ) as r:
                    tsv_path.write_bytes(r.read())
                log(f"dev.tsv: {tsv_path.stat().st_size} bytes")
            except (urllib.error.URLError, TimeoutError) as exc:
                log(f"dev.tsv FAILED: {exc}")
                failures.append(locale)
                continue

        transcripts = parse_transcripts(tsv_path.read_text(encoding="utf-8"))
        log(f"{len(transcripts)} transcripts available")

        if not download(
            f"{HF}/data/{locale}/audio/dev.tar.gz",
            tar_path,
            EXPECTED_DEV_TARBALL.get(locale),
        ):
            failures.append(locale)
            continue

        # Clips that actually have a transcript: a clip with an empty reference
        # cannot be scored, so it is not a fixture.
        usable = [stem for stem, (_cid, text) in transcripts.items() if text.strip()]
        usable = usable[: args.per_locale]
        clips = extract_clips(tar_path, set(usable), args.out, transcripts, locale)
        log(f"extracted {len(clips)} clip(s)")
        manifest.extend(clips)
        if len(clips) < args.per_locale:
            failures.append(f"{locale} (only {len(clips)}/{args.per_locale} clips)")

    if not args.keep_archive:
        for locale in locales:
            (args.cache / f"{locale}_dev.tar.gz").unlink(missing_ok=True)

    # The manifest is what the phone reads. It records the Taiwanese gap
    # explicitly so the report cannot imply coverage that was never tested.
    meta = {
        "source": "google/fleurs",
        "split": "dev",
        "locales": {k: LOCALES[k] for k in locales},
        "not_tested": {
            "TTS": ["tw", "ta", "my"],
            "reason": (
                "piper has no Taiwanese Mandarin, Tamil or Malay voice in any of "
                "its 177 voices; its locale list contains hi and te but not ta, "
                "ms or yue. STT covers these via the multilingual model."
            ),
            "STT": ["tw"],
            "stt_reason": (
                "FLEURS has mainland Mandarin (cmn_hans_cn) and Cantonese "
                "(yue_hant_hk) but no Taiwanese Mandarin; Cantonese is a "
                "different language, not a Taiwanese accent."
            ),
        },
        "clips": manifest,
    }
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n==> DONE")
    print(f"  manifest {manifest_path}")
    print(f"  clips    {len(manifest)}")
    by_locale: dict[str, int] = {}
    for clip in manifest:
        by_locale[clip["locale"]] = by_locale.get(clip["locale"], 0) + 1
    for locale, count in sorted(by_locale.items()):
        print(f"    {locale:14} {count}")
    if failures:
        print(f"  PROBLEMS: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
