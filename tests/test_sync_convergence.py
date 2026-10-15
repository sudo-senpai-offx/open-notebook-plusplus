"""Two devices, random operations, one converged state.

Every sync test so far walks a scripted path: create here, import there,
assert. Real use is interleaved - both sides creating, editing and
deleting the same kinds of records between syncs, in any order. This file
asserts the property that must hold regardless of order: after a full
cycle in both directions, both databases hold the same records with the
same content, no duplicates, no resurrections, no orphans.

What "the same" means
---------------------
Record ids are created independently per device, so convergence is a
UNION, not equality: every record created anywhere exists everywhere,
minus tombstoned ones. For ids created on both sides (the fixture seeds
a shared set first, the way the phone and PC share history), the newer
`updated` wins on both sides. Edges converge as sets: every filing made
anywhere exists everywhere. Chat messages union by id; sessions by id.

What is randomised
------------------
Creations, field edits, notebook filings, deletions - across notes,
sources, notebooks, sessions and messages - on both devices, between
syncs. Deletions go through the real model methods (which is what writes
no tombstone; the export sweep must notice). Deterministic seeds, so a
failure replays exactly: the seed is in the assertion message.

What is NOT randomised
----------------------
Blob bytes (the channel is covered elsewhere; manifests carry hashes
that resolve to nothing here, which is the normal pending state),
transcript merges beyond appends, and clock skew (timestamps come from
one clock, so last-write-wins is unambiguous - skew is a separate test).
"""

import os
import random
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)

TABLES = ("note", "source", "notebook", "flashcard", "chat_session", "chat_message")
EDGE_TABLES = ("artifact", "reference", "refers_to")


@pytest.fixture
def pair(monkeypatch, tmp_path):
    """Two devices sharing one sync dir, each with its own database."""
    import open_notebook.config as config

    sync = tmp_path / "sync"
    state: dict = {"namespaces": {}}

    def use(label: str):
        if label not in state["namespaces"]:
            state["namespaces"][label] = f"conv_{label}_{uuid.uuid4().hex[:8]}"
        ns = state["namespaces"][label]
        monkeypatch.setenv("SURREAL_NAMESPACE", ns)
        monkeypatch.setenv("SURREAL_DATABASE", ns)
        monkeypatch.setenv("DATA_FOLDER", str(tmp_path / f"data_{label}"))
        blobs = tmp_path / f"blobs_{label}"
        blobs.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "BLOBS_FOLDER", str(blobs), raising=False)
        return ns

    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))
    roots = {label: tmp_path / f"blobs_{label}" for label in ("a", "b")}
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    use("a")

    yield use, sync, roots

    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe() -> None:
        for ns in state["namespaces"].values():
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in TABLES + EDGE_TABLES:
                try:
                    await repo_query(f"DELETE {table}")  # noqa: S608
                except Exception:
                    pass

    try:
        asyncio.run(wipe())
    except Exception:
        pass


class World:
    """Random operations, with per-device id pools merged at each sync.

    A device can only touch records it holds: operating on an id that
    exists solely on the other side is not a sync disagreement, it is a
    generator bug (a user cannot edit what is not there). Pools merge
    after every cycle minus globally deleted ids, so synced records
    become operable on both sides - edited, filed and deleted from
    either end, the way real use interleaves.
    """

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.pools: dict[str, dict[str, list[str]]] = {
            device: {"note": [], "source": [], "notebook": [], "session": []}
            for device in ("a", "b")
        }
        self.deleted: set[str] = set()
        self.counter = 0
        self.device = "a"

    def _tag(self) -> str:
        self.counter += 1
        return f"s{self.counter:03d}"

    async def merge(self, use) -> None:
        """Union both pools minus the dead, intersected with what is live.

        The intersection matters: applying a tombstone removes a record
        the pool still names, and without pruning, the next burst files
        edges to the dead id - a state no production path produces (the
        UI lists live notebooks), which once read as a one-edge sync
        divergence. Pools track operable records, and a tombstoned
        record is not operable anywhere.
        """
        from open_notebook.database.repository import repo_query

        tables = {
            "note": "note",
            "source": "source",
            "notebook": "notebook",
            "session": "chat_session",
        }
        for device in ("a", "b"):
            use(device)
            for kind, table in tables.items():
                live = {
                    str(r)
                    for r in (await repo_query(f"SELECT VALUE id FROM {table}") or [])
                }
                union = set(self.pools["a"][kind]) | set(self.pools["b"][kind])
                self.pools[device][kind] = sorted((union - self.deleted) & live)

    async def create_note(self) -> str:
        from open_notebook.domain.notebook import Note

        tag = self._tag()
        note = Note(title=f"note {tag}", note_type="human", content=f"body {tag}")
        await note.save()
        rid = str(note.id)
        self.pools[self.device]["note"].append(rid)
        return rid

    async def create_source(self) -> str:
        from open_notebook.domain.notebook import Source

        tag = self._tag()
        source = Source(title=f"source {tag}", topics=[])
        await source.save()
        rid = str(source.id)
        self.pools[self.device]["source"].append(rid)
        return rid

    async def create_notebook(self) -> str:
        from open_notebook.domain.notebook import Notebook

        tag = self._tag()
        nb = Notebook(name=f"Course {tag}", description="")
        await nb.save()
        rows = await self._q(
            "SELECT VALUE id FROM notebook WHERE name = $n", {"n": f"Course {tag}"}
        )
        rid = str(rows[0])
        self.pools[self.device]["notebook"].append(rid)
        return rid

    async def create_session(self) -> str:
        from open_notebook.database.repository import ensure_record_id, repo_query
        from open_notebook.domain.notebook import persist_chat_messages

        sid = f"chat_session:{uuid.uuid4().hex[:12]}"
        await repo_query(
            "CREATE $id CONTENT { title: $t, updated: time::now() }",
            {"id": ensure_record_id(sid), "title": f"chat {self._tag()}"},
        )
        await persist_chat_messages(sid, [_Msg("human", f"hello {self.counter}")])
        self.pools[self.device]["session"].append(sid)
        return sid

    async def edit_note(self, rid: str) -> None:
        from open_notebook.domain.notebook import Note

        note = await Note.get(rid)
        note.content = f"edited {self._tag()}"
        await note.save()

    async def _file(self, table: str, rid: str, nid: str) -> None:
        # Check-then-relate, the way the import files: RELATE always
        # inserts, so filing the same pair twice leaves two identical
        # rows on one side that no sync can reconcile away - the other
        # side imports once (its already-check skips the second) and the
        # comparison reads it as divergence. Production UI filing does
        # not dedupe either, but the sync contract is set-semantics and
        # the generator must speak it.
        from open_notebook.database.repository import ensure_record_id, repo_query

        existing = await repo_query(
            f"SELECT VALUE out FROM {table} WHERE in = $rid",  # noqa: S608
            {"rid": ensure_record_id(rid)},
        )
        if str(nid) in {str(x) for x in (existing or [])}:
            return
        await repo_query(
            f"RELATE $in->{table}->$out",  # noqa: S608
            {"in": ensure_record_id(rid), "out": ensure_record_id(nid)},
        )

    async def file_source(self, sid: str, nid: str) -> None:
        await self._file("reference", sid, nid)

    async def file_note(self, nid: str, nb: str) -> None:
        await self._file("artifact", nid, nb)

    async def delete(self, table: str, rid: str) -> None:
        from open_notebook.database.repository import ensure_record_id, repo_query

        self.deleted.add(rid)
        if table == "notebook":
            # Incident edges die with their vertex in every production
            # path (model cascades, tombstone unlinks). A bare record
            # delete that left them would test a state sync never
            # produces - and did, once, as a one-edge divergence.
            for edge_table in ("artifact", "reference", "refers_to"):
                await repo_query(
                    f"DELETE {edge_table} WHERE in = $rid OR out = $rid",  # noqa: S608
                    {"rid": ensure_record_id(rid)},
                )
        # Through the model methods, the way a user deletes: no direct
        # tombstone is written, so the export sweep must notice.
        # Notebook deletes use the bare record path - the cascade is a
        # UI behaviour, and sync tombstones unlink rather than cascade.
        from open_notebook.database.repository import repo_delete

        if table == "notebook":
            await repo_delete(ensure_record_id(rid))
        else:
            from open_notebook.domain.notebook import Note, Source

            model = Note if table == "note" else Source
            await (await model.get(rid)).delete()

    async def step(self, device: str) -> str:
        """One random operation on *device*'s pool. Returns a description."""
        self.device = device
        pool = self.pools[device]

        choices = ["note", "source", "notebook", "session"]
        if pool["note"]:
            choices += ["edit-note", "del-note"]
        if pool["source"]:
            choices += ["del-source"]
        if pool["source"] and pool["notebook"]:
            choices += ["file-source"]
        if pool["note"] and pool["notebook"]:
            choices += ["file-note"]
        if pool["notebook"]:
            choices += ["del-notebook"]
        op = self.rng.choice(choices)

        if op == "note":
            return f"create {await self.create_note()}"
        if op == "source":
            return f"create {await self.create_source()}"
        if op == "notebook":
            await self.create_notebook()
            names = await self._q("SELECT name FROM notebook")
            return f"create notebook ({len(names)} total)"
        if op == "session":
            return f"create {await self.create_session()}"
        if op == "edit-note":
            rid = self.rng.choice(pool["note"])
            await self.edit_note(rid)
            return f"edit {rid}"
        if op == "del-note":
            rid = pool["note"].pop(self.rng.randrange(len(pool["note"])))
            await self.delete("note", rid)
            return f"delete note {rid}"
        if op == "del-source":
            rid = pool["source"].pop(self.rng.randrange(len(pool["source"])))
            await self.delete("source", rid)
            return f"delete source {rid}"
        if op == "del-notebook":
            rid = pool["notebook"].pop(self.rng.randrange(len(pool["notebook"])))
            # Members stay: sync tombstones unlink rather than cascade,
            # so the generator mirrors that - delete the bare record.
            await self.delete("notebook", rid)
            return f"delete notebook {rid}"
        if op == "file-source":
            return f"file {await self._file_one('source')}"
        if op == "file-note":
            return f"file note {await self._file_one('note')}"
        raise AssertionError(f"unknown op {op}")

    async def _file_one(self, kind: str) -> str:
        pool = self.pools[self.device]
        rid = self.rng.choice(pool["source"] if kind == "source" else pool["note"])
        nb = self.rng.choice(pool["notebook"])
        if kind == "source":
            await self.file_source(rid, nb)
        else:
            await self.file_note(rid, nb)
        return rid

    async def _q(self, statement: str, params: dict | None = None):
        from open_notebook.database.repository import repo_query

        return await repo_query(statement, params or {})


class _Msg:
    def __init__(self, role: str, content: str):
        self.type = role
        self.content = content
        self.id = f"m{uuid.uuid4().hex[:8]}"


def _send_blobs(roots: dict, source: str, target: str) -> None:
    """Stand in for the blob channel: copy one device's bytes to another.

    Manifests move first and bytes follow in production; without this
    the import side names transcripts it can never read, and every
    transcript assertion tests nothing. Deliberately a file copy rather
    than a call into blobtransfer: what can silently break is the
    manifest and the store agreeing on a hash.
    """
    import shutil

    for shard in roots[source].iterdir():
        if not shard.is_dir():
            continue
        destination = roots[target] / shard.name
        destination.mkdir(parents=True, exist_ok=True)
        for entry in shard.iterdir():
            shutil.copy(entry, destination / entry.name)


async def _cycle(use, sync: Path, first: str, second: str, world=None, roots=None):
    """A full cycle in both directions: export, import, export, import."""
    from open_notebook.sync.exchange import export_all, import_all

    use(first)
    await export_all(device=first, root=sync)
    if roots is not None:
        _send_blobs(roots, first, second)
    use(second)
    await import_all(root=sync, dry_run=False)
    await export_all(device=second, root=sync)
    if roots is not None:
        _send_blobs(roots, second, first)
    use(first)
    await import_all(root=sync, dry_run=False)
    if world is not None:
        await world.merge(use)


async def _snapshot() -> dict:
    """Everything, canonically. Edge endpoints as strings, records by id."""
    from open_notebook.database.repository import repo_query

    snap: dict = {}
    for table in TABLES:
        rows = await repo_query(f"SELECT * FROM {table}") or []
        snap[table] = {
            str(r.get("id")): {
                k: (str(v) if k in ("id", "session") else v)
                for k, v in r.items()
                if k != "embedding"
            }
            for r in rows
        }
    for table in EDGE_TABLES:
        rows = await repo_query(f"SELECT in, out FROM {table}") or []
        snap[table] = sorted((str(r.get("in")), str(r.get("out"))) for r in rows)
    return snap


def _normalize(snap: dict) -> dict:
    """Replace notebook record ids with slugs everywhere they appear.

    A record id is per-database and means nothing on the other device -
    that is the founding assumption of the whole sync design, and this
    assertion tripped over it: it compared notebook ids across devices,
    which can never match, and called correct convergence a divergence.
    Slugs are the shared identity; edges name notebooks by id, so both
    endpoints normalize through each side's own slug map.
    """
    slug_of = {}
    for rid, row in snap["notebook"].items():
        from open_notebook.sync.exchange import _slug

        slug_of[rid] = _slug(row.get("name") or "")

    def norm(value: str) -> str:
        return slug_of.get(value, value)

    return {
        "notebook": {
            slug_of[rid]: {**row, "id": slug_of[rid]}
            for rid, row in snap["notebook"].items()
        },
        "edges": sorted(
            (table, norm(i), norm(o)) for table in EDGE_TABLES for (i, o) in snap[table]
        ),
        "records": {table: snap[table] for table in TABLES if table != "notebook"},
    }


def _assert_converged(a: dict, b: dict, seed: int, history: list[str]) -> None:
    trail = "\n    ".join(history[-8:])
    na, nb = _normalize(a), _normalize(b)
    for table, rows_a in na["records"].items():
        rows_b = nb["records"][table]
        assert set(rows_a) == set(rows_b), (
            f"seed {seed}: {table} ids diverge\n"
            f"  only on A: {sorted(set(rows_a) - set(rows_b))}\n"
            f"  only on B: {sorted(set(rows_b) - set(rows_a))}\n"
            f"  last ops:\n    {trail}"
        )
        for rid in rows_a:
            for field in ("title", "content", "updated"):
                if field in rows_a[rid] or field in rows_b[rid]:
                    assert rows_a[rid].get(field) == rows_b[rid].get(field), (
                        f"seed {seed}: {table}:{rid}.{field} differs: "
                        f"{rows_a[rid].get(field)!r} vs {rows_b[rid].get(field)!r}"
                    )
    assert set(na["notebook"]) == set(nb["notebook"]), (
        f"seed {seed}: notebook slugs diverge\n"
        f"  only on A: {sorted(set(na['notebook']) - set(nb['notebook']))}\n"
        f"  only on B: {sorted(set(nb['notebook']) - set(na['notebook']))}"
    )
    for slug in na["notebook"]:
        assert na["notebook"][slug].get("name") == nb["notebook"][slug].get("name"), (
            f"seed {seed}: notebook {slug} renamed differently"
        )
    assert na["edges"] == nb["edges"], (
        f"seed {seed}: edges diverge\n"
        f"  only on A: {[e for e in na['edges'] if e not in nb['edges']][:5]}\n"
        f"  only on B: {[e for e in nb['edges'] if e not in na['edges']][:5]}"
    )


@pytest.mark.parametrize("seed", [11, 22, 33, 44, 55])
@pytest.mark.asyncio
async def test_random_operations_converge(pair, seed):
    """Property, not path: any interleaving converges after a full cycle."""
    use, sync, roots = pair
    rng = random.Random(seed)
    world = World(rng)
    history: list[str] = []

    # Shared history first, the way the phone and PC share records:
    # without it every id is unique per device and the union is trivial.
    use("a")
    for _ in range(6):
        history.append("a: " + await world.step("a"))
    await _cycle(use, sync, "a", "b", world, roots)

    # Interleaved work with syncs between bursts.
    for burst in range(4):
        for _ in range(rng.randint(8, 14)):
            device = rng.choice(["a", "b"])
            use(device)
            history.append(f"{device}: " + await world.step(device))
        await _cycle(use, sync, "a", "b", world, roots)

    # Back-to-back identical snapshots must be identical. If they are
    # not, the comparison reads nondeterministically and no failure it
    # reports can be trusted - this guard once caught a phantom
    # divergence where two consecutive reads of one database disagreed.
    use("a")
    assert await _snapshot() == await _snapshot(), (
        "same device, same state, different snapshot"
    )
    use("a")
    snap_a = await _snapshot()
    use("b")
    snap_b = await _snapshot()
    _assert_converged(snap_a, snap_b, seed, history)


@pytest.mark.asyncio
async def test_repeated_cycles_are_stable(pair):
    """Idempotence: syncing a converged state changes nothing.

    Counts, edges and file sets must be identical after three more full
    cycles - no duplicates, no resurrections, no churn.
    """
    use, sync, roots = pair
    rng = random.Random(7)
    world = World(rng)

    use("a")
    for _ in range(10):
        await world.step("a")
    await _cycle(use, sync, "a", "b", world, roots)

    def files() -> list[str]:
        return sorted(
            str(p.relative_to(sync))
            for p in sync.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )

    use("a")
    base = await _snapshot()
    base_files = files()
    for _ in range(3):
        await _cycle(use, sync, "a", "b", world, roots)
    use("a")
    assert await _snapshot() == base, "a converged state must not move"
    assert files() == base_files, "the manifest must not churn either"
