"""Linking a source to a notebook.

The bug this pins: the idempotency check in the link route compared
``out = $source_id AND in = $notebook_id`` while the ``RELATE`` directly
below it creates ``in = source, out = notebook``. The two are opposites,
so the check never matched an existing edge and every call created
another one.

It was invisible because SurrealDB happily accepts a second identical
``RELATE``. Nothing errors, the link still works, and the only symptom
is a slowly growing pile of duplicate edges - visible as sources that
appear twice in a notebook and retrieval that double-counts them.
"""

import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not __import__("os").getenv("SURREAL_TEST_URL"),
    reason="needs a live SurrealDB; set SURREAL_TEST_URL",
)


@pytest.fixture
def device(monkeypatch):
    issued = []

    def use(label: str) -> str:
        ns = f"notebooklink_{label}_{uuid.uuid4().hex[:8]}"
        issued.append(ns)
        monkeypatch.setenv("SURREAL_NAMESPACE", ns)
        monkeypatch.setenv("SURREAL_DATABASE", ns)
        return ns

    monkeypatch.setenv("SURREAL_URL", __import__("os").environ["SURREAL_TEST_URL"])
    monkeypatch.setenv(
        "SURREAL_USER", __import__("os").getenv("SURREAL_TEST_USER", "root")
    )
    monkeypatch.setenv(
        "SURREAL_PASSWORD", __import__("os").getenv("SURREAL_TEST_PASSWORD", "root")
    )
    use("a")
    yield use

    import asyncio

    from open_notebook.database.repository import repo_query

    async def wipe_all() -> None:
        for ns in issued:
            monkeypatch.setenv("SURREAL_NAMESPACE", ns)
            monkeypatch.setenv("SURREAL_DATABASE", ns)
            for table in ("source", "notebook", "reference"):
                try:
                    await repo_query(f"DELETE {table}")
                except Exception:
                    pass

    try:
        asyncio.run(wipe_all())
    except Exception:
        pass


async def _edge_count(source_id: str, notebook_id: str) -> int:
    from open_notebook.database.repository import ensure_record_id, repo_query

    rows = await repo_query(
        "SELECT VALUE id FROM reference WHERE in = $sid AND out = $nid",
        {"sid": ensure_record_id(source_id), "nid": ensure_record_id(notebook_id)},
    )
    return len(rows or [])


@pytest.mark.asyncio
async def test_linking_twice_creates_one_edge(device):
    """The regression: `in` is the source, `out` is the notebook."""
    from api.routers.notebooks import add_source_to_notebook
    from open_notebook.domain.notebook import Notebook, Source

    notebook = Notebook(name="Molecular Biology", description="")
    await notebook.save()
    source = Source(title="Genomic library", topics=[])
    await source.save()

    await add_source_to_notebook(str(notebook.id), str(source.id))
    await add_source_to_notebook(str(notebook.id), str(source.id))

    assert await _edge_count(str(source.id), str(notebook.id)) == 1


@pytest.mark.asyncio
async def test_the_edge_direction_is_what_readers_assume(device):
    """`export_all` and `fn::vector_search` both assume this orientation.

    `export_all` selects `out` where `in` is the source, and
    `fn::vector_search` scopes with `SELECT VALUE in FROM reference WHERE
    out IN $notebook_ids`. An edge stored the other way round is
    invisible to both.
    """
    from api.routers.notebooks import add_source_to_notebook
    from open_notebook.database.repository import ensure_record_id, repo_query
    from open_notebook.domain.notebook import Notebook, Source

    notebook = Notebook(name="Hematology", description="")
    await notebook.save()
    source = Source(title="Blood film", topics=[])
    await source.save()

    await add_source_to_notebook(str(notebook.id), str(source.id))

    by_source = await repo_query(
        "SELECT VALUE out FROM reference WHERE in = $sid",
        {"sid": ensure_record_id(str(source.id))},
    )
    assert [str(x) for x in (by_source or [])] == [str(notebook.id)]

    scoped = await repo_query(
        "SELECT VALUE in FROM reference WHERE out = $nid",
        {"nid": ensure_record_id(str(notebook.id))},
    )
    assert [str(x) for x in (scoped or [])] == [str(source.id)]
