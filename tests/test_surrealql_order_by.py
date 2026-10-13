"""Guards against `ORDER BY` on a field absent from an explicit projection.

SurrealQL 2.x rejects the statement at parse time:

    Parse error: Missing order idiom `created` in statement selection

It is a parse error, not a runtime one, so the query never reaches the
database and `repo_query` raises - which the flashcards export handler
catches as a bare `except Exception` and reports as
`{"detail": "Failed to export deck"}`, HTTP 500.

`SELECT *` is unaffected because `*` already includes the sort field.
Only explicit projections are at risk, and the export was the only such
query in the tree.

No test caught it: tests/test_flashcard_endpoints.py stubs `repo_query`,
so the query string was never handed to SurrealDB. It surfaced only when
the export was actually run on a device. This check is therefore static -
it reads the query text and compares it to the ORDER BY clause, so it runs
with no database and fails the moment a projection and its sort diverge.
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# A single SurrealQL statement, possibly split across Python string literals.
QUERY_LITERAL = re.compile(r"(?P<q>[\"'][^\"']*(?:\"[^\"']*\"[^\"']*)*[\"'])")
SELECT_CLAUSE = re.compile(
    r"SELECT\s+(?P<cols>[^;]*?)\s+FROM\b", re.IGNORECASE | re.DOTALL
)
ORDER_BY = re.compile(
    r"ORDER\s+BY\s+(?P<fields>[^;]*?)(?:\s+LIMIT|\s+START|\s+FETCH|$)",
    re.IGNORECASE | re.DOTALL,
)
STRING_PREFIXES = ("f", "b", "rf", "fr", "F", "B", "RF", "FR")


def _source_files() -> list[Path]:
    roots = ["api", "open_notebook", "commands"]
    files: list[Path] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in str(p))
    return sorted(files)


def _iter_queries(path: Path):
    """Yield (line_number, query_text) for every plausible SurrealQL literal.

    f-strings need care. Since Python 3.12 an f-string parses to a
    `JoinedStr` whose literal chunks are separate `Constant` nodes, so
    walking for `Constant` alone yields fragments. notebook.py's
    `select *{note_projection} from ... order by note.updated desc` would
    arrive as `"select *"` plus a second chunk that still contains SELECT
    but has lost the `*`, and be reported as a violation that does not
    exist. So: rebuild the whole f-string, and ignore its chunks.
    """
    text = path.read_text()
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - repo should always parse
        return

    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inside_fstring.update(id(part) for part in node.values)
            joined = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if re.search(r"\bSELECT\b", joined, re.IGNORECASE):
                yield node.lineno, " ".join(joined.split())

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in inside_fstring
            and re.search(r"\bSELECT\b", node.value, re.IGNORECASE)
        ):
            yield node.lineno, " ".join(node.value.split())


def _unselected_sort_fields(query: str) -> list[str]:
    select = SELECT_CLAUSE.search(query)
    order = ORDER_BY.search(query)
    if not select or not order:
        return []

    columns = [c.strip() for c in select.group("cols").split(",")]
    # `SELECT *` and `SELECT * OMIT a.b` both project every field, so the
    # sort field is always present. Matches notebook.py's `select *{omit}`.
    if any(c.startswith("*") for c in columns):
        return []

    selected: set[str] = set()
    for col in columns:
        # strip an alias and any table qualifier: `flashcard.created AS c`
        head = re.split(r"\s+AS\s+", col, flags=re.IGNORECASE)[0]
        selected.add(head.strip().split(".")[-1].lower())

    missing = []
    for raw in order.group("fields").split(","):
        field = raw.strip().split()[0] if raw.strip() else ""
        field = field.split(".")[-1].strip("`").lower()
        if field and field not in selected:
            missing.append(field)
    return missing


class TestSurrealQlOrderByProjection:
    def test_flashcard_export_projection_includes_created(self):
        """The query that 500'd on device, pinned explicitly.

        Extracted through the AST rather than by slicing the function body:
        the query is two adjacent string literals
        (`"SELECT ... guid, created "` + `"FROM flashcard ..."`), so matching
        the raw source text never sees a contiguous `SELECT ... FROM` and the
        check silently passes on anything.
        """
        queries = [
            q
            for _, q in _iter_queries(REPO_ROOT / "api" / "routers" / "flashcards.py")
            # the export is the only flashcard query with an explicit
            # projection; list_cards uses `SELECT *`
            if "FROM flashcard" in q and "ORDER BY" in q and "SELECT *" not in q
        ]
        assert len(queries) == 1, f"expected one export query, found {len(queries)}"
        export = queries[0]
        assert "ORDER BY created" in export, export
        missing = _unselected_sort_fields(export)
        assert missing == [], (
            f"flashcard export sorts by {missing} but does not select it. "
            "SurrealQL 2.x raises a parse error for this, and the handler "
            "reports it as 'Failed to export deck' (HTTP 500)."
        )

    def test_no_query_orders_by_an_unselected_field(self):
        offenders: list[str] = []
        for path in _source_files():
            for lineno, query in _iter_queries(path):
                missing = _unselected_sort_fields(query)
                if missing:
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(
                        f"{rel}:{lineno} orders by {missing} but selects ..."
                    )

        assert not offenders, (
            "SurrealQL 2.x parse-errors when ORDER BY names a field absent "
            "from an explicit projection. Either add the field to the SELECT "
            "list or use SELECT *:\n  " + "\n  ".join(offenders)
        )


@pytest.mark.parametrize(
    "query", ["SELECT * FROM x ORDER BY created", "SELECT id FROM x"]
)
def test_helper_is_not_fooled_by_select_star(query):
    assert _unselected_sort_fields(query) == []
