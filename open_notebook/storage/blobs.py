"""Content-addressed storage for source files.

Why this exists
---------------
The previous layout wrote every upload to ``data/uploads/<name>.pdf``,
reserving a unique name with a ``(1)``, ``(2)`` counter when one already
existed. Two consequences:

1. **Duplicates.** The same PDF added to two notebooks, or re-added after
   an edit, landed on disk twice. 258 sources on the operator's PC already
   carry a `title` collision counter in their filenames, which is the
   symptom of this.
2. **Names are not identity.** ``report.pdf`` means nothing across
   devices, so any sync of files has to trust the sender's path.

Addressing by content hash fixes both at once. The same bytes always
resolve to the same path, so a duplicate costs nothing and needs no
lookup: the second write finds the file already there. And the hash is
the identity that crosses devices - a manifest entry naming ``sha256``
means the same thing on every machine, which a path cannot.

Layout
------
``<root>/<first two hex chars>/<full hash><suffix>``

Sharded two levels deep because a flat directory of tens of thousands of
entries is pathological on the filesystems that matter here - ``sdcardfs``
on Android in particular. The suffix is preserved because callers branch
on it: ``_assert_file_supported()`` and the PDF text-layer probe in
``graphs/source.py`` both test the extension of ``asset.file_path``, and
keeping it there means neither has to change.

Atomicity
---------
Writes go to a temporary file in the destination directory and are then
``os.replace``d into place, which is atomic on POSIX. Note that content
addressing makes the racing case harmless in a way the old counter loop
could not be: two writers computing the same hash produce the *same*
bytes, so whichever wins the replace, the result is correct. There is no
claim to lose and no window where a reader sees a partial file.

This is why the old ``generate_unique_filename()`` TOCTOU dance can go:
it existed because "is this name free?" and "claim the name" were
separate steps. With a deterministic name they are the same step.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# Read size for hashing and copying. Large enough that a 50 MB PDF is
# not read in Python-level chunks, small enough to stay off the heap.
_CHUNK = 1024 * 1024

# Suffixes are only used for extension-sniffing by callers, so anything
# exotic is rejected rather than sanitised into something misleading.
_MAX_SUFFIX = 16


@dataclass(frozen=True)
class BlobRef:
    """Where a file ended up, and what it is.

    ``path`` stays on the Asset so every existing consumer - extraction,
    the PDF probe, the download route, delete cleanup - keeps working
    unchanged. The hash and the original name are what make the record
    meaningful beyond this machine.
    """

    sha256: str
    size: int
    path: str
    filename: str
    #: True when the bytes were already present and nothing was written.
    deduped: bool


def _root(root: Path | str | None = None) -> Path:
    """Resolve the blob root, defaulting to the configured data folder.

    Imported lazily because ``config`` imports this module indirectly at
    startup; a module-level import would be circular.
    """
    if root is not None:
        return Path(root)
    from open_notebook.config import BLOBS_FOLDER

    return Path(BLOBS_FOLDER)


def _safe_suffix(filename: str) -> str:
    """Return a conservative file extension for blob storage.

    Only the final component is considered, so ``../../evil.pdf`` cannot
    steer the write. Multiple suffixes are dropped rather than joined
    (``.tar.gz`` -> ``.gz``); nothing downstream distinguishes them and a
    compound suffix invites surprise.
    """
    suffix = Path(os.path.basename(filename or "")).suffix
    if not suffix or len(suffix) > _MAX_SUFFIX or "/" in suffix or "\\" in suffix:
        return ""
    return suffix.lower()


def blob_path_for(
    sha256: str, suffix: str = "", root: Path | str | None = None
) -> Path:
    """Return the canonical on-disk path for a hash.

    Pure: does not touch the filesystem. Callers that need the file to
    exist should check with :func:`blob_exists` or just call
    :func:`store_bytes`, which does both.
    """
    digest = _validate_hash(sha256)
    return _root(root) / digest[:2] / f"{digest}{suffix.lower()}"


def _validate_hash(sha256: str) -> str:
    """Reject anything that is not a lowercase hex sha256.

    The hash is used to build a path. A value containing ``/`` or ``..``
    would escape the blob root, and a value that is not a digest could
    silently collide with a real one, so this is checked rather than
    assumed. Callers get the hash from :func:`store_bytes`, which always
    produces a valid one - this guards the paths where a hash arrives
    from a manifest or a sync peer instead.
    """
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError(f"Not a sha256 digest: {sha256!r}")
    if sha256 != sha256.lower():
        raise ValueError("sha256 must be lowercase hex")
    try:
        int(sha256, 16)
    except ValueError:
        raise ValueError(f"Not hex: {sha256!r}") from None
    return sha256


def blob_exists(sha256: str, root: Path | str | None = None) -> bool:
    """True when a blob with this hash is present under *any* suffix."""
    digest = _validate_hash(sha256)
    shard = _root(root) / digest[:2]
    if not shard.is_dir():
        return False
    return any(shard.glob(f"{digest}*"))


def resolve_blob_path(
    sha256: str | None,
    filename: str | None = None,
    root: Path | str | None = None,
) -> str | None:
    """Find the local file for a hash, or None when the bytes are absent.

    This is the half of sync that needs no network. A source imported
    from another device arrives as a hash with no ``file_path`` — giving
    it the *other* device's path would name a file that does not exist
    here. So the path is derived from the hash each time it is needed and
    resolves to None until a blob transfer brings the bytes.

    The suffix is tried first when the original filename is known, then
    without one, so a blob stored before extensions were preserved is
    still found.
    """
    if not sha256:
        return None
    try:
        digest = _validate_hash(sha256)
    except ValueError:
        return None

    suffix = _safe_suffix(filename or "")
    exact = _root(root) / digest[:2] / f"{digest}{suffix}"
    if exact.is_file():
        return str(exact)

    shard = _root(root) / digest[:2]
    if shard.is_dir():
        for candidate in sorted(shard.glob(f"{digest}.*")):
            if candidate.is_file():
                return str(candidate)
    return None


def _hash_stream(file_obj: object) -> tuple[str, int]:
    """Hash a binary file object, returning ``(hexdigest, byte_count)``."""
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = file_obj.read(_CHUNK)  # type: ignore[attr-defined]
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def store_bytes(
    content: bytes, filename: str, root: Path | str | None = None
) -> BlobRef:
    """Store *content* once and return where it landed.

    Hashing happens before anything is written, so the common case -
    a file already present - costs one read and no disk writes at all.
    """
    sha256 = hashlib.sha256(content).hexdigest()
    suffix = _safe_suffix(filename)
    final = blob_path_for(sha256, suffix, root)

    if final.exists():
        # Same bytes, already stored. Re-verify rather than trusting the
        # name: the file may have been truncated by a crash mid-write on
        # an older build, and a wrong hash is worse than a missing file.
        if _file_hash(final) == sha256:
            return BlobRef(
                sha256=sha256,
                size=len(content),
                path=str(final),
                filename=filename,
                deduped=True,
            )
        logger.warning(f"Blob {final} is corrupt for its hash; rewriting")
        final.unlink()

    final.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(final, content)

    return BlobRef(
        sha256=sha256,
        size=len(content),
        path=str(final),
        filename=filename,
        deduped=False,
    )


def store_file(
    source_path: Path | str,
    filename: str | None = None,
    root: Path | str | None = None,
) -> BlobRef:
    """Ingest a file that already exists on this filesystem.

    This is the path for content the user already has - a PDF in their
    downloads folder, a file an extraction step just produced. It is
    copied in rather than referenced, because a reference breaks the
    moment the user moves or deletes the original, and a source that
    cannot be opened is worse than one extra copy.

    When the source already lives inside the blob root it is not copied
    at all; the hash is taken and the existing path is returned, so
    re-ingesting a blob is a no-op rather than a self-copy.
    """
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(f"Not a file: {src}")

    name = filename or src.name
    root_dir = _root(root).resolve()
    try:
        already_inside = src.resolve().is_relative_to(root_dir)
    except (OSError, ValueError):
        already_inside = False

    if already_inside:
        sha256, size = _file_hash_and_size(src)
        return BlobRef(
            sha256=sha256,
            size=size,
            path=str(src),
            filename=name,
            deduped=True,
        )

    suffix = _safe_suffix(name)
    final = _root(root) / "incoming"
    final.mkdir(parents=True, exist_ok=True)

    # Hash and stage in one pass: a second read of a large PDF to compute
    # the digest would double the ingest cost for no benefit.
    staged: Path | None = None
    try:
        with src.open("rb") as handle:
            fd, tmp_name = tempfile.mkstemp(dir=final, prefix="stage-", suffix=suffix)
            staged = Path(tmp_name)
            with os.fdopen(fd, "wb") as out:
                digest = hashlib.sha256()
                size = 0
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
        sha256 = digest.hexdigest()
        target = blob_path_for(sha256, suffix, root)
        if target.exists() and _file_hash(target) == sha256:
            staged.unlink(missing_ok=True)
            return BlobRef(
                sha256=sha256,
                size=size,
                path=str(target),
                filename=name,
                deduped=True,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        staged = None
        return BlobRef(
            sha256=sha256,
            size=size,
            path=str(target),
            filename=name,
            deduped=False,
        )
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _atomic_write(target: Path, content: bytes) -> None:
    """Write *content* so readers see either the old file or the new one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        sha256, _ = _hash_stream(handle)
        return sha256


def _file_hash_and_size(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        return _hash_stream(handle)


async def is_referenced(sha256: str, exclude_source: str | None = None) -> bool:
    """True when any *other* source still points at this blob.

    Dedup makes the old unconditional ``os.unlink`` on source delete a
    data-loss bug: two sources sharing a blob means deleting either one
    unlinks bytes the other still needs. The old code could not hit this
    because every file had a unique name.

    Fails safe. If the query cannot run - database down, record malformed -
    this reports "referenced", so the caller keeps the file. Leaking an
    orphan blob is recoverable; deleting study material because a
    database was briefly unreachable is not.
    """
    from open_notebook.database.repository import ensure_record_id, repo_query

    query = "SELECT count() AS n FROM source WHERE asset.sha256 = $sha GROUP ALL"
    params: dict[str, object] = {"sha": sha256}
    if exclude_source:
        query = (
            "SELECT count() AS n FROM source "
            "WHERE asset.sha256 = $sha AND id != $exclude GROUP ALL"
        )
        # RecordID, not str. `id != $exclude` with a string parameter
        # never matches in SurrealDB 2.x - the comparison is against a
        # record id, so the filter silently matches nothing and every
        # source counts as its own referrer. That direction fails safe,
        # but it means blobs are never collected: a silent disk leak.
        # Verified against a live 2.7.0 server, where the string form
        # returned 2 and the RecordID form returned 1.
        params["exclude"] = ensure_record_id(exclude_source)
    try:
        rows = await repo_query(query, params)
    except Exception as e:
        logger.warning(f"Reference check failed for {sha256}; keeping blob: {e}")
        return True
    return bool(rows and rows[0].get("n"))


async def unlink_if_unreferenced(
    blob_path: str, sha256: str, source_id: str | None = None
) -> bool:
    """Delete *blob_path* only when no other source still needs it.

    The hash is passed separately from the path on purpose. They are not
    interchangeable: ``is_referenced`` matches on ``asset.sha256``, so
    handing it a path yields a query that can never match and reports
    "unreferenced" for a shared blob - which would delete bytes another
    source still reads.

    Returns True when the file was removed. Safe to call on a path that
    is already gone - deleting a source whose file the extraction step
    already cleaned up must not raise.
    """
    if await is_referenced(sha256, exclude_source=source_id):
        return False
    try:
        Path(blob_path).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning(f"Failed to delete blob {blob_path}: {e}")
        return False
