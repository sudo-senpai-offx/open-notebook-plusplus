"""Submitting jobs into the `held` state.

Why not `surreal_commands.submit_command`
----------------------------------------
It hardcodes `status: "new"`, and `new` means *claimable right now*. A job
written there is picked up by the vendor worker's live subscription before any
admission decision can be made about it, so every gate in this package would be
decorative. Writing the row ourselves is the only way the state means what it
says.

The cost is having to reproduce two things the vendor helper does, and both
matter more than they look:

**Registry validation.** `registry.get_command_by_id` plus
`input_schema(**args).model_dump()`. Without it, a malformed argument reaches
the worker, passes its own `app.name` formatting check, and fails deep inside
LangGraph - where the retry budget is generous enough that one typo burns
dozen-plus attempts before it is finally marked failed. Validating here turns
that into an immediate, attributable error.

**`created`.** The vendor worker claims jobs with
`ORDER BY created ASC`, on the assumption that field is populated. It is not:
nothing in this project or in the package ever sets it, and there is no
`DEFINE TABLE command` in the migrations, so the column exists only in
projections and reads back null. The oldest-first ordering has therefore never
had anything to order by, and which job the worker happened to pick was
arbitrary. This module sets it, which is what makes the ordering real and what
lets the dispatcher promote in submission order rather than at random.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional

from loguru import logger

APP = "open_notebook"


def _validate(app: str, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Registry lookup and schema validation, matching the vendor helper.

    Imported lazily: the registry is populated by importing the command
    modules, and pulling that in at module scope would make this file
    expensive to import from the hot path that only wants the status list.
    """
    from surreal_commands.core.registry import registry

    command_id = f"{app}.{command}"
    registry_item = registry.get_command_by_id(command_id)
    if not registry_item:
        raise ValueError(f"Command not found: {command_id}")
    return dict(registry_item.input_schema(**args).model_dump())


def submit_held(
    app: str,
    command: str,
    args: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """Queue a job as `held`. Returns the new command id.

    Synchronous, like the vendor helper it replaces, because every call site
    in this project is already synchronous and converting them all would be a
    much larger change than the one this is fixing.

    Connection settings come from the vendor's own `command_service`
    singleton rather than from this project's repository module, so the
    namespace and database are the same ones the worker and the vendor helper
    use. Reading them from a second source is how a submitter ends up writing
    jobs to a database nobody is watching.
    """
    from surreal_commands.core.service import command_service
    from surrealdb import Surreal

    validated = _validate(app, command, args)

    with Surreal(command_service.db_url) as db:
        db.signin(command_service.db_auth)
        db.use(command_service.db_namespace, command_service.db_database)
        result = db.create(
            "command",
            {
                "app": app,
                "name": command,
                "args": validated,
                "context": context or {},
                "status": "held",
                # ISO-8601, matching what the rest of this codebase stores and
                # what `ORDER BY` compares correctly on. `time` is SurrealDB's
                # own record of creation; `created` is what the worker and the
                # dispatcher actually sort by.
                "created": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
        )

    # `create` returns the created record, which SurrealDB types loosely enough
    # that mypy refuses to index it directly - the value could be bytes.
    record_id = result.get("id") if isinstance(result, dict) else None
    if record_id is None:
        raise RuntimeError(f"queue accepted {app}.{command} but returned no id")
    command_id = str(record_id)
    logger.debug(f"submitted held: {app}.{command} -> {command_id}")
    return command_id


def submit_held_safe(
    app: str,
    command: str,
    args: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """As `submit_held`, but returns None instead of raising.

    For call sites where a queueing failure must not take down the operation
    that triggered it - a source upload, a sync import. The caller gets a
    missing job rather than an exception, which it can report as "extraction
    did not start" instead of failing the user's request outright.
    """
    try:
        return submit_held(app, command, args, context)
    except Exception as exc:  # noqa: BLE001 - one job never sinks the caller
        logger.warning(f"could not queue {app}.{command} as held: {exc}")
        return None
