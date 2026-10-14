"""Every migration file on disk must actually be registered.

The up/down lists in `AsyncMigrationManager` are hand-maintained. A
`.surrealql` file that nobody references is committed, reviewed, and
silently never runs - the schema it was supposed to create simply does
not exist, and the first symptom is whatever breaks later.

This is the same shape as the `~/bin` wrappers drifting from
`scripts/termux-*.sh`: a file that exists in one place and is wired up
in another. The wire-up is the part that rots.

Migration 36 was exactly this: `chat_message` was defined, and the
table did not exist.
"""

import re
from pathlib import Path

import pytest

MANAGER = (
    Path(__file__).resolve().parents[1] / "open_notebook/database/async_migrate.py"
)
MIGRATIONS = Path(__file__).resolve().parents[1] / "open_notebook/database/migrations"


def _referenced() -> set[str]:
    text = MANAGER.read_text()
    return set(re.findall(r"migrations/(\d+(?:_down)?\.surrealql)", text))


def _on_disk() -> set[str]:
    return {p.name for p in MIGRATIONS.glob("*.surrealql")}


class TestRegistration:
    def test_the_manager_file_exists(self):
        assert MANAGER.is_file(), "the hardcoded migration lists moved"

    @pytest.mark.parametrize(
        "name", sorted(_on_disk()), ids=lambda n: n.replace(".surrealql", "")
    )
    def test_every_file_is_registered(self, name: str):
        unreferenced = _on_disk() - _referenced()
        assert name not in unreferenced, (
            f"{name} exists but is not listed in AsyncMigrationManager, so it "
            "never runs. Adding the file is not enough."
        )

    def test_there_are_no_stale_references(self):
        """A listed file that does not exist fails the run outright."""
        dangling = _referenced() - _on_disk()
        assert not dangling, f"registered but missing on disk: {sorted(dangling)}"

    def test_up_and_down_are_paired(self):
        up = {n for n in _on_disk() if not n.endswith("_down.surrealql")}
        down = {n for n in _on_disk() if n.endswith("_down.surrealql")}
        missing_down = {n.replace(".surrealql", "_down.surrealql") for n in up} - down
        assert not missing_down, f"no rollback for: {sorted(missing_down)}"

    def test_migrations_are_numbered_without_gaps(self):
        """A gap means a migration was renumbered and never re-run anywhere."""
        numbers = sorted(
            int(n.split(".")[0])
            for n in _on_disk()
            if not n.endswith("_down.surrealql")
        )
        assert numbers == list(range(1, len(numbers) + 1)), (
            f"expected 1..{len(numbers)}, found {numbers}"
        )
