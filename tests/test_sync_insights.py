"""Insights travel like notes: small text, stable links, no bytes.

Model-generated per source (transformation output), insights cost
inference time to regenerate - minutes next to two llama-servers on a
phone - while transporting them costs bytes in git. So they sync;
embeddings stay local (vectors are model-specific and the two devices
run different models).
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)


@pytest.fixture
def two_devices(monkeypatch, tmp_path):
    issued: list[str] = []
    by_label: dict[str, str] = {}

    def use(label: str) -> str:
        if label not in by_label:
            by_label[label] = f"insights_{label}_{uuid.uuid4().hex[:8]}"
            issued.append(by_label[label])
        ns = by_label[label]
        monkeypatch.setenv("SURREAL_NAMESPACE", ns)
        monkeypatch.setenv("SURREAL_DATABASE", ns)
        monkeypatch.setenv("DATA_FOLDER", str(tmp_path / f"data_{label}"))
        return ns

    monkeypatch.setenv("SURREAL_URL", os.environ["SURREAL_TEST_URL"])
    monkeypatch.setenv("SURREAL_USER", os.getenv("SURREAL_TEST_USER", "root"))
    monkeypatch.setenv("SURREAL_PASSWORD", os.getenv("SURREAL_TEST_PASSWORD", "root"))
    use("a")

    yield use

    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe() -> None:
        for ns in issued:
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in ("source_insight", "source"):
                try:
                    await repo_query(f"DELETE {table}")  # noqa: S608
                except Exception:
                    pass

    try:
        asyncio.run(wipe())
    except Exception:
        pass


async def _source(title: str) -> str:
    from open_notebook.domain.notebook import Source

    source = Source(title=title, topics=[])
    await source.save()
    return str(source.id)


async def _insight(source_id: str, kind: str, content: str) -> str:
    from open_notebook.database.repository import ensure_record_id, repo_query

    rid = f"source_insight:{uuid.uuid4().hex[:12]}"
    await repo_query(
        "CREATE $id CONTENT { insight_type: $kind, content: $content, "
        "source: $src, updated: time::now() }",
        {
            "id": ensure_record_id(rid),
            "kind": kind,
            "content": content,
            "src": ensure_record_id(source_id),
        },
    )
    return rid


async def _all_insight_ids() -> list[str]:
    from open_notebook.database.repository import repo_query

    return [
        str(r) for r in (await repo_query("SELECT VALUE id FROM source_insight") or [])
    ]


async def _get_insight(record_id: str) -> dict | None:
    from open_notebook.database.repository import ensure_record_id, repo_query

    rows = await repo_query("SELECT * FROM $id", {"id": ensure_record_id(record_id)})
    return dict(rows[0]) if rows else None


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_an_insight_arrives_linked(self, two_devices, tmp_path):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        sid = await _source("Paper")
        iid = await _insight(sid, "Dense Summary", "the paper argues X")
        await export_all("pc", root=root)

        use("b")
        result = await import_all(root=root, dry_run=False)

        assert result["insights_created"] == 1
        arrived = await _get_insight(iid)
        assert arrived is not None, "the insight must arrive under its own id"
        assert arrived["content"] == "the paper argues X"
        assert arrived["insight_type"] == "Dense Summary"
        assert str(arrived["source"]) == sid, "linked to the same source id"

    @pytest.mark.asyncio
    async def test_a_newer_edit_wins(self, two_devices, tmp_path):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.database.repository import ensure_record_id, repo_query
        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        sid = await _source("Paper")
        iid = await _insight(sid, "Key Insights", "version one")
        await export_all("pc", root=root)

        use("b")
        await import_all(root=root, dry_run=False)
        await repo_query(
            "UPDATE $id SET content = $c, updated = time::now()",
            {"id": ensure_record_id(iid), "c": "version two"},
        )
        await export_all("phone", root=root)

        use("a")
        result = await import_all(root=root, dry_run=False)

        assert result["insights_updated"] == 1
        arrived = await _get_insight(iid)
        assert arrived is not None
        assert arrived["content"] == "version two"

    @pytest.mark.asyncio
    async def test_an_insight_for_an_unknown_source_still_imports(
        self, two_devices, tmp_path
    ):
        """Ordering must not destroy content: the source may arrive on a
        later cycle, and deleting the insight for arriving first would
        be sync eating its own."""
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.sync.exchange import export_all, import_all

        use("a")
        iid = await _insight("source:never-here", "Key Insights", "orphaned")
        await export_all("pc", root=root)

        use("b")
        result = await import_all(root=root, dry_run=False)

        assert result["insights_created"] == 1
        arrived = await _get_insight(iid)
        assert arrived is not None
        assert arrived["content"] == "orphaned"


class TestDeletion:
    @pytest.mark.asyncio
    async def test_a_deleted_insight_is_tombstoned(self, two_devices, tmp_path):
        use = two_devices
        root = tmp_path / "sync"

        from open_notebook.database.repository import repo_query
        from open_notebook.sync import tombstones
        from open_notebook.sync.exchange import export_all

        use("a")
        sid = await _source("Paper")
        await _insight(sid, "Key Insights", "temporary")
        await export_all("pc", root=root)
        assert list((root / "insights").glob("*.json"))

        from open_notebook.database.repository import ensure_record_id as _rid

        await repo_query(
            "DELETE source_insight WHERE id = $id",
            {"id": _rid((await _all_insight_ids())[0])},
        )
        manifest = await export_all("pc", root=root)

        assert manifest["counts"]["insights"] == 0
        assert manifest["counts"]["tombstoned"] >= 1
        assert not list((root / "insights").glob("*.json"))
        assert tombstones.load_tombstones(root), "the peer must be told"

    @pytest.mark.asyncio
    async def test_applying_an_insight_tombstone_removes_only_it(
        self, two_devices, tmp_path
    ):
        """The record goes; its source stays. A tombstone is forget-this,
        and the source is a different record with its own lifecycle."""
        use = two_devices
        use("b")
        root = tmp_path / "sync"
        root.mkdir(parents=True)

        from open_notebook.database.repository import ensure_record_id, repo_query
        from open_notebook.sync import tombstones
        from open_notebook.sync.exchange import _apply_tombstones

        sid = await _source("Keeps living")
        iid = await _insight(sid, "Key Insights", "doomed")
        assert await _get_insight(iid) is not None

        tombstones.write_tombstone(
            root, iid, "insights", previous_updated="2099-01-01T00:00:00+00:00"
        )
        result: dict = {
            "tombstones_applied": 0,
            "tombstones_overridden_by_local_edit": 0,
            "tombstones_already_absent": 0,
            "errors": [],
        }
        await _apply_tombstones(root, result, dry_run=False)

        assert result["tombstones_applied"] == 1
        assert await _get_insight(iid) is None
        rows = await repo_query(
            "SELECT VALUE id FROM source WHERE id = $id",
            {"id": ensure_record_id(sid)},
        )
        assert rows, "the source outlives its insight's tombstone"
