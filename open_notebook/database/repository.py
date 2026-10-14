import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypeVar, Union

from loguru import logger
from surrealdb import AsyncSurreal, RecordID  # type: ignore
from surrealdb.data.types.table import Table  # type: ignore

from open_notebook.utils.proxy import ensure_internal_no_proxy

# Keep the internal SurrealDB websocket out of any configured HTTP proxy
# (issue #1160). Runs at import time - i.e. before any db_connection() can be
# opened - so it protects every entrypoint (API + worker) that touches the DB.
ensure_internal_no_proxy()

T = TypeVar("T", Dict[str, Any], List[Dict[str, Any]])

# Bare SurrealDB table/relation identifier: no ':', whitespace, or query
# syntax. Used to validate the parts of RELATE/UPSERT/UPDATE that name a
# table or edge-relation and therefore can't be bound as a query parameter
# (SurrealQL only allows binding record/table *values*, not identifiers in
# that position).
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _ensure_safe_identifier(value: str, kind: str) -> str:
    """Validate a table/relationship name before it is interpolated into a query."""
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid {kind} name: {value!r}")
    return value


def _get_env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def get_database_url() -> str:
    """Get database URL with backward compatibility"""
    surreal_url = os.getenv("SURREAL_URL")
    if surreal_url:
        return surreal_url

    # Fallback to old format - WebSocket URL format
    address = os.getenv("SURREAL_ADDRESS", "localhost")
    port = os.getenv("SURREAL_PORT", "8000")
    return f"ws://{address}/rpc:{port}"


def get_database_password() -> str:
    """Get password with backward compatibility"""
    return os.getenv("SURREAL_PASSWORD") or os.getenv("SURREAL_PASS") or "root"


def get_database_namespace() -> str:
    """Get configured SurrealDB namespace."""
    return _get_env_or_default("SURREAL_NAMESPACE", "open_notebook")


def get_database_name() -> str:
    """Get configured SurrealDB database name."""
    return _get_env_or_default("SURREAL_DATABASE", "open_notebook")


def parse_record_ids(obj: Any) -> Any:
    """Recursively parse and convert RecordIDs into strings."""
    if isinstance(obj, dict):
        return {k: parse_record_ids(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [parse_record_ids(item) for item in obj]
    elif isinstance(obj, RecordID):
        return str(obj)
    return obj


def ensure_record_id(value: Union[str, RecordID]) -> RecordID:
    """Ensure a value is a RecordID."""
    if isinstance(value, RecordID):
        return value
    return RecordID.parse(value)


@asynccontextmanager
async def db_connection():
    db = AsyncSurreal(get_database_url())
    await db.signin(
        {
            "username": os.environ.get("SURREAL_USER"),
            "password": get_database_password(),
        }
    )
    await db.use(get_database_namespace(), get_database_name())
    try:
        yield db
    finally:
        await db.close()


# --------------------------------------------------------------------------
# destructive-query guard
# --------------------------------------------------------------------------
#
# An unfiltered table delete is the one query in this codebase that can
# destroy a library. It happened: `DELETE source` was run against the
# operator's real namespace while clearing a scratch one, because the
# namespace environment variable was left unset and defaulted to
# `open_notebook`. 258 source records and 2,754 embeddings were lost,
# and the database compacted them before a stop could be attempted.
#
# Every legitimate delete in this codebase is already scoped - by record
# id, a WHERE clause, or a bound parameter - so refusing the unscoped
# shape costs nothing and blocks the one query that cannot be undone.

# Matches the target of a DELETE that is not immediately constrained by a
# WHERE. `$id`, `type::thing(...)` and a trailing parameter all bind a
# single record and are deliberately not matched.
_UNSCOPED_DELETE = re.compile(
    r"\bDELETE\s+(?:FROM\s+)?(?P<table>[a-z_][a-z0-9_]*)\s*(?P<rest>;|$)",
    re.IGNORECASE,
)

# A delete that names a bound parameter as its target deletes at most one
# record: `DELETE $id`, `DELETE type::thing('_sbl_migrations', $v)`.
_BOUND_TARGET = re.compile(r"^\s*(?:type::thing\s*\(|\$)", re.IGNORECASE)

# Namespaces that exist to be thrown away. Test fixtures and the local
# scratch database may wipe a table without ceremony.
_SCRATCH_NAMESPACE = re.compile(
    r"^(?:tmp|test|synctest|scratch|throwaway)", re.IGNORECASE
)

# Deliberate opt-out, for the rare case where a bulk delete really is
# intended on real data (a documented data purge, say).
ALLOW_BULK_DELETE_ENV = "OPEN_NOTEBOOK_ALLOW_BULK_DELETE"


class UnscopedDeleteError(RuntimeError):
    """Raised when a delete would remove every row in a table.

    Subclasses RuntimeError so existing `except Exception` handlers around
    repo_query behave as they did before, rather than introducing a new
    failure mode in callers that never expected one.
    """


def guard_destructive_query(query: str, namespace: str | None = None) -> None:
    """Refuse a table-wide DELETE unless it is demonstrably safe.

    Scoped deletes pass untouched. An unfiltered table delete raises,
    unless the namespace looks like scratch space or the operator has
    explicitly opted in via the environment.
    """
    if os.getenv(ALLOW_BULK_DELETE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return

    ns = namespace if namespace is not None else get_database_namespace()
    if _SCRATCH_NAMESPACE.match(ns or ""):
        return

    for match in _UNSCOPED_DELETE.finditer(query or ""):
        table = match.group("table")
        rest = query[match.end("rest") :]
        if "WHERE" in rest.upper():
            continue
        if _BOUND_TARGET.match(rest):
            continue
        raise UnscopedDeleteError(
            f"Refusing to run unscoped DELETE against table {table!r} in "
            f"namespace {ns!r}. A delete with no WHERE removes every row "
            "in the table and cannot be undone. Scope it with a WHERE "
            "clause, run it against a scratch namespace, or set "
            f"{ALLOW_BULK_DELETE_ENV}=1 if this is genuinely intended."
        )


async def repo_query(
    query_str: str, vars: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Execute a SurrealQL query and return the results"""

    guard_destructive_query(query_str)

    async with db_connection() as connection:
        try:
            result = parse_record_ids(await connection.query(query_str, vars))
            if isinstance(result, str):
                raise RuntimeError(result)
            return result
        except RuntimeError as e:
            # RuntimeError is raised for retriable transaction conflicts - log at debug to avoid noise
            logger.debug(str(e))
            raise
        except Exception as e:
            logger.exception(e)
            raise


async def repo_create(table: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new record in the specified table"""
    # Remove 'id' attribute if it exists in data
    data.pop("id", None)
    data["created"] = datetime.now(timezone.utc)
    data["updated"] = datetime.now(timezone.utc)
    try:
        async with db_connection() as connection:
            result = parse_record_ids(await connection.insert(table, data))
            # SurrealDB may return a string error message instead of the expected record
            if isinstance(result, str):
                raise RuntimeError(result)
            return result
    except RuntimeError as e:
        logger.error(str(e))
        raise
    except Exception as e:
        logger.exception(e)
        raise RuntimeError("Failed to create record")


async def repo_relate(
    source: Union[str, RecordID],
    relationship: str,
    target: Union[str, RecordID],
    data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Create a relationship between two records with optional data"""
    if data is None:
        data = {}
    # relationship is an edge-table name, not a record value, so it can't be
    # bound as a query parameter; validate it against an identifier allowlist
    # instead of trusting the caller. source/target are always bound.
    _ensure_safe_identifier(relationship, "relationship")
    query = f"RELATE $source->{relationship}->$target CONTENT $data;"
    # logger.debug(f"Relate query: {query}")

    return await repo_query(
        query,
        {
            "source": ensure_record_id(source),
            "target": ensure_record_id(target),
            "data": data,
        },
    )


async def repo_upsert(
    table: str, id: Optional[str], data: Dict[str, Any], add_timestamp: bool = False
) -> List[Dict[str, Any]]:
    """Create or update a record in the specified table"""
    data.pop("id", None)
    if add_timestamp:
        data["updated"] = datetime.now(timezone.utc)
    _ensure_safe_identifier(table, "table")
    target: Union[RecordID, Table] = ensure_record_id(id) if id else Table(table)
    query = "UPSERT $target MERGE $data;"
    return await repo_query(query, {"target": target, "data": data})


async def repo_update(
    table: str, id: Union[str, RecordID], data: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Update an existing record by table and id"""
    # If id already contains the table name, use it as is
    try:
        _ensure_safe_identifier(table, "table")
        if isinstance(id, RecordID):
            record_id: RecordID = id
        elif ":" in id and id.startswith(f"{table}:"):
            record_id = ensure_record_id(id)
        else:
            record_id = RecordID(table, id)
        data.pop("id", None)
        if "created" in data and isinstance(data["created"], str):
            data["created"] = datetime.fromisoformat(data["created"])
        data["updated"] = datetime.now(timezone.utc)
        query = "UPDATE $target MERGE $data;"
        # logger.debug(f"Update query: {query}")
        result = await repo_query(query, {"target": record_id, "data": data})
        # if isinstance(result, list):
        #     return [_return_data(item) for item in result]
        return parse_record_ids(result)
    except Exception as e:
        raise RuntimeError(f"Failed to update record: {str(e)}")


async def repo_delete(record_id: Union[str, RecordID]):
    """Delete a record by record id"""

    try:
        async with db_connection() as connection:
            return await connection.delete(ensure_record_id(record_id))
    except Exception as e:
        logger.exception(e)
        raise RuntimeError(f"Failed to delete record: {str(e)}")


async def repo_insert(
    table: str,
    data: List[Dict[str, Any]],
    ignore_duplicates: bool = False,
    batch_size: int = 50,
) -> List[Dict[str, Any]]:
    """Insert records in bounded batches using a single database connection."""
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]

    results = []
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    async with db_connection() as connection:
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            try:
                result = parse_record_ids(await connection.insert(table, chunk))
                # SurrealDB may return a string error message instead of the expected records
                if isinstance(result, str):
                    raise RuntimeError(result)
                if isinstance(result, list):
                    results.extend(result)
                elif result is not None:
                    results.append(result)
            except RuntimeError as e:
                if ignore_duplicates and "already contains" in str(e):
                    continue
                # Log transaction conflicts at debug level (they are expected during concurrent operations)
                error_str = str(e).lower()
                if "transaction" in error_str or "conflict" in error_str:
                    logger.debug(str(e))
                else:
                    logger.error(str(e))
                raise
            except Exception as e:
                if ignore_duplicates and "already contains" in str(e):
                    continue
                logger.exception(e)
                raise RuntimeError("Failed to create record")
    return results
