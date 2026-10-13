"""Guards against SurrealDB's silent object-drop on SCHEMAFULL tables.

SurrealDB 2.7.0 accepts an object value written to a field on a SCHEMAFULL
table when that field has no nested FIELD definitions, reports OK, and
stores `{}`. Nothing in the response, the API layer, or the Pydantic model
shows the loss - `record_grade` computed a correct FSRS schedule, returned
it to the caller, and the card stayed due forever because the write landed
empty.

Measured on 2.7.0, SCHEMAFULL table, object value written:

    TYPE object, no nested defs   -> {}       lost
    TYPE any                      -> {}       lost
    TYPE object + nested defs     -> stored
    FLEXIBLE TYPE object          -> stored

No test caught it because tests/test_review_store.py stubs `repo_query`,
so the write path was never exercised against a real database. It was
found by running the review flow on a phone. These tests therefore check
the migration SQL itself, which needs no database and fails fast.
"""

import re
from pathlib import Path

MIGRATIONS_DIR = (
    Path(__file__).parent.parent / "open_notebook" / "database" / "migrations"
)

# `DEFINE FIELD [IF NOT EXISTS] [OVERWRITE] <name> ON [TABLE] <table>
#  (FLEXIBLE)? TYPE <type>;`  -- the parts this test cares about.
#
# <name> is [^\s;]+ rather than \w+ so nested declarations like
# `speakers.*.name` are captured. Without those, `speaker_profile.speakers`
# (declared TYPE array<object> plus four `speakers.*.*` fields in migration
# 7) looks like an undeclared object field when it is in fact fully
# declared - and its stored rows really do keep every key.
DEFINE_FIELD = re.compile(
    r"DEFINE\s+FIELD\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:OVERWRITE\s+)?"
    r"(?P<field>[^\s;]+)\s+ON\s+(?:TABLE\s+)?(?P<table>[\w.]+)\s+"
    r"(?P<flexible>FLEXIBLE\s+)?"
    r"TYPE\s+(?P<type>[^;]+);",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    """Drop `-- ...` line comments.

    Migration 33 quotes the broken declaration inside its explanation.
    Without this, the comment parses as a second still-broken definition
    and the migration that fixes the bug appears to reintroduce it.
    """
    return re.sub(r"--[^\n]*", "", sql)


def _up_migrations() -> list[Path]:
    """Up migrations in numeric order - later ones supersede earlier ones."""
    paths = [
        p
        for p in MIGRATIONS_DIR.glob("*.surrealql")
        if not p.name.endswith("_down.surrealql")
    ]
    return sorted(paths, key=lambda p: int(p.name.split(".")[0]))


def _effective_defs() -> dict[tuple[str, str], tuple[str, re.Match[str]]]:
    """Last definition wins, keyed by (table, field).

    Migration 32 declared `fsrs_json TYPE option<object>` and migration 33
    overwrites it with FLEXIBLE. Only the surviving definition describes
    current behaviour, so only that one may fail the check.
    """
    effective: dict[tuple[str, str], tuple[str, re.Match[str]]] = {}
    for path in _up_migrations():
        for match in DEFINE_FIELD.finditer(_strip_comments(path.read_text())):
            key = (match.group("table").lower(), match.group("field").lower())
            effective[key] = (path.name, match)
    return effective


def _declares_object(type_sql: str) -> bool:
    """Whether this type accepts an object value that can be silently emptied.

    `object` and `any` both take a dict; `record`, `array` of scalars and the
    scalar kinds do not have this failure mode. `option<object>` matches
    because the inner `object` does - that was the shape that broke.
    """
    bare = type_sql.replace(" ", "").lower()
    return any(
        re.search(rf"(^|[<>]){word}($|[<>])", bare) for word in ("object", "any")
    )


def _has_nested_defs(field: str, effective: dict) -> bool:
    """Whether `<field>.*` or `<field>.<sub>` is declared somewhere."""
    prefix = f"{field}."
    return any(name.startswith(prefix) for _, name in effective)


class TestObjectFieldsAreNotSilentlyDropped:
    def test_fsrs_json_is_flexible_and_allows_none(self):
        """The specific field that broke, pinned so it cannot regress.

        Both halves are load-bearing. FLEXIBLE stops the content being
        emptied; `option<>` keeps the field absent-able. `FLEXIBLE TYPE
        object` alone stores objects correctly and still *rejects* the
        write - and `_prepare_save_data` omits every None field, so a card
        created before its first review has no fsrs_json at all.
        """
        sql = (MIGRATIONS_DIR / "33.surrealql").read_text()
        assert (
            "DEFINE FIELD OVERWRITE fsrs_json ON TABLE flashcard "
            "FLEXIBLE TYPE option<object>" in sql
        ), "33.surrealql must declare fsrs_json FLEXIBLE TYPE option<object>"

    def test_migration_33_is_registered(self):
        """Migrations are hard-coded in the manager, not discovered.

        Searches rather than indexing [-1]: this test originally assumed 33
        was the newest migration and broke the moment 34 landed. A future
        migration should not require editing an unrelated test.
        """
        from open_notebook.database.async_migrate import AsyncMigrationManager

        manager = AsyncMigrationManager()
        assert len(manager.up_migrations) == len(manager.down_migrations)
        needle = "fsrs_json ON TABLE flashcard FLEXIBLE TYPE option<object>"
        assert any(needle in m.sql for m in manager.up_migrations), (
            "migration 33 (the fsrs_json FLEXIBLE fix) is not registered"
        )
        assert any(
            "REMOVE FIELD IF EXISTS fsrs_json ON TABLE flashcard" in m.sql
            for m in manager.down_migrations
        )

    def test_no_unflexible_object_field_without_nested_defs(self):
        """No SCHEMAFULL field may hold an object without FLEXIBLE or nested defs.

        FLEXIBLE is the escape hatch that makes SurrealDB store the value as
        given; declaring nested fields also works. Anything else accepts the
        write, reports OK, and stores `{}`.
        """
        effective = _effective_defs()
        offenders = []
        for (table, field), (name, match) in effective.items():
            if not _declares_object(match.group("type")):
                continue
            if match.group("flexible"):
                continue
            if _has_nested_defs(field, effective):
                continue
            offenders.append(
                f"{name}: {table}.{field} TYPE {match.group('type').strip()}"
            )

        assert not offenders, (
            "These fields accept an object but store {} on a SCHEMAFULL "
            "table (SurrealDB 2.7.0). Add FLEXIBLE, or declare nested "
            "fields:\n  " + "\n  ".join(sorted(offenders))
        )
