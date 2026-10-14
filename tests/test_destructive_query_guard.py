"""The unscoped-delete guard.

A `DELETE source` was run against the operator's real namespace while
clearing a scratch one, because the namespace env var was unset and
fell back to `open_notebook`. 258 sources and 2,754 embeddings were
gone, compacted before a stop could be attempted, and 213 of them are
unrecoverable.

So the guard exists. These tests pin both halves of that: the dangerous
shape is refused, and every scoped delete the application actually
performs keeps working. The second half matters more - a guard that
blocks normal use gets removed, and then it protects nothing.
"""

import pytest

from open_notebook.database.repository import (
    ALLOW_BULK_DELETE_ENV,
    UnscopedDeleteError,
    guard_destructive_query,
)

REAL = "open_notebook"


class TestRefusesTheDangerousShape:
    @pytest.mark.parametrize(
        "query",
        [
            "DELETE source",
            "DELETE source;",
            "DELETE FROM source",
            "delete from flashcard",
            "DELETE note",
            "DELETE source_embedding",
            "USE NS x; DELETE revlog",
        ],
    )
    def test_table_wide_delete_is_refused(self, query, monkeypatch):
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        with pytest.raises(UnscopedDeleteError):
            guard_destructive_query(query, namespace=REAL)

    def test_the_error_names_the_table_and_namespace(self, monkeypatch):
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        with pytest.raises(UnscopedDeleteError) as exc:
            guard_destructive_query("DELETE source", namespace=REAL)
        message = str(exc.value)
        assert "source" in message
        assert REAL in message
        # Must tell the operator how to proceed, not just refuse.
        assert "WHERE" in message
        assert ALLOW_BULK_DELETE_ENV in message


class TestLegitimateDeletesStillPass:
    """Every scoped delete the codebase actually issues."""

    @pytest.mark.parametrize(
        "query",
        [
            # From open_notebook/domain/notebook.py
            "DELETE artifact WHERE out = $notebook_id",
            "DELETE reference WHERE out = $notebook_id",
            "DELETE source_embedding WHERE source = $source_id",
            "DELETE source_insight WHERE source = $source_id",
            # From open_notebook/maintenance/prune.py
            "DELETE FROM source WHERE created < $cutoff",
            # From api/routers/notebooks.py
            "DELETE FROM reference WHERE out = $notebook_id AND in = $source_id",
            # From open_notebook/database/async_migrate.py
            "DELETE type::thing('_sbl_migrations', $version);",
        ],
    )
    def test_scoped_deletes_are_allowed(self, query, monkeypatch):
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        guard_destructive_query(query, namespace=REAL)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM source",
            "UPSERT $rid MERGE $data RETURN AFTER",
            "CREATE source SET title = 'x'",
            "UPDATE source SET last_viewed_at = time::now()",
            "RELATE $sid->artifact->$nid",
        ],
    )
    def test_non_deletes_are_untouched(self, query, monkeypatch):
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        guard_destructive_query(query, namespace=REAL)

    def test_delete_by_bound_record_id_is_allowed(self, monkeypatch):
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        guard_destructive_query("DELETE $id", namespace=REAL)


class TestEscapeHatches:
    @pytest.mark.parametrize(
        "ns", ["tmp", "test", "synctest_a1b2", "scratch", "throwaway_db"]
    )
    def test_scratch_namespaces_may_wipe(self, ns, monkeypatch):
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        guard_destructive_query("DELETE source", namespace=ns)

    def test_explicit_opt_in_unblocks_real_namespaces(self, monkeypatch):
        monkeypatch.setenv(ALLOW_BULK_DELETE_ENV, "1")
        guard_destructive_query("DELETE source", namespace=REAL)

    def test_opt_in_is_not_triggered_by_nonsense(self, monkeypatch):
        monkeypatch.setenv(ALLOW_BULK_DELETE_ENV, "maybe")
        with pytest.raises(UnscopedDeleteError):
            guard_destructive_query("DELETE source", namespace=REAL)

    def test_namespace_prefix_must_be_at_the_start(self, monkeypatch):
        # "my_test_db" is a real-looking namespace, not a scratch one.
        monkeypatch.delenv(ALLOW_BULK_DELETE_ENV, raising=False)
        with pytest.raises(UnscopedDeleteError):
            guard_destructive_query("DELETE source", namespace="my_test_db")


class TestGuardIsWiredIn:
    @pytest.mark.asyncio
    async def test_repo_query_runs_the_guard(self, monkeypatch):
        """The guard must be inside repo_query, not merely importable.

        Otherwise a caller could reach the database without passing it,
        which is exactly the path the original mistake took. No database
        is required to tell the two apart: with the guard in place the
        call refuses before opening a connection, and without it the
        call fails trying to connect.
        """
        import open_notebook.database.repository as repo

        monkeypatch.delenv(repo.ALLOW_BULK_DELETE_ENV, raising=False)
        monkeypatch.setenv("SURREAL_NAMESPACE", REAL)

        with pytest.raises(repo.UnscopedDeleteError):
            await repo.repo_query("DELETE source")

    @pytest.mark.asyncio
    async def test_scoped_delete_passes_the_guard(self, monkeypatch):
        """A scoped delete gets past the guard - proof by reaching the wire."""
        import open_notebook.database.repository as repo

        monkeypatch.delenv(repo.ALLOW_BULK_DELETE_ENV, raising=False)
        monkeypatch.setenv("SURREAL_NAMESPACE", REAL)
        # Point at a closed port rather than relying on SURREAL_URL being
        # absent. When a developer or CI exports a live URL, the query
        # succeeds, nothing raises, and this test fails for a reason that
        # has nothing to do with the guard - which is how a battery stops
        # being worth reading.
        monkeypatch.setenv("SURREAL_URL", "ws://127.0.0.1:9/rpc")

        # No UnscopedDeleteError: whatever happens next is the database
        # being unavailable, which is exactly the point - the guard let
        # a legitimate delete through.
        with pytest.raises(Exception) as exc:
            await repo.repo_query("DELETE source_embedding WHERE source = $x")
        assert not isinstance(exc.value, repo.UnscopedDeleteError)
