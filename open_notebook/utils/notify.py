"""Job lifecycle notifications (B3 seam, D1 ntfy transport).

The seam is deliberately tiny: job lifecycle sites call
``notify_job_event(event, job)`` and never care how (or whether) the event is
delivered. Behaviour:

* Always log the event (structured, one line).
* If ``OPEN_NOTEBOOK_NOTIFY_URL`` is unset -> **zero footprint**: no DB read,
  no task, no HTTP client. Offline stays offline.
* If set -> resolve the human subject (source/notebook name) best-effort, then
  fire-and-forget an ntfy POST (5 s timeout, optional bearer token). A send
  failure is a single ``warning`` and never touches job status.

Unit tests mock ``httpx`` — no network.
"""

import asyncio
import json
import os
from typing import Any, Dict, Literal, Optional, Set
from urllib.parse import quote

import httpx
from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.utils.ssl_config import httpx_verify_setting

JobEvent = Literal["completed", "failed"]

#: Fire-and-forget POSTs in flight (kept referenced so they are not GC'd).
_PENDING: Set["asyncio.Task[None]"] = set()


def _notify_url() -> Optional[str]:
    url = os.getenv("OPEN_NOTEBOOK_NOTIFY_URL", "").strip()
    return url or None


def _extract_args(job: Dict) -> Dict[str, Any]:
    """``command.args`` may be a dict or a JSON string depending on the driver."""
    args = job.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return {}
    return args if isinstance(args, dict) else {}


def _subject_path(record_id: str, kind: str) -> str:
    # Match the frontend, which does encodeURIComponent(id) on route ids.
    return f"/{kind}/{quote(str(record_id), safe='')}"


async def _resolve_subject(args: Dict) -> Optional[Dict[str, str]]:
    """Best-effort ``{"name", "path"}`` for the job's source or notebook.

    Never raises: a missing name falls back to the record id, and a DB error
    returns ``None`` so the event still goes out with a generic body.
    """
    source_id = args.get("source_id")
    if source_id:
        sid = str(source_id)
        try:
            rows = await repo_query(
                "SELECT title FROM $id", {"id": ensure_record_id(sid)}
            )
            name = rows[0].get("title") if rows else None
        except Exception:
            name = None
        return {"name": str(name or sid), "path": _subject_path(sid, "sources")}

    notebook_ids = args.get("notebook_ids")
    if not notebook_ids and args.get("notebook_id"):
        notebook_ids = [args["notebook_id"]]
    if notebook_ids:
        nid = str(notebook_ids[0])
        try:
            rows = await repo_query(
                "SELECT name FROM $id", {"id": ensure_record_id(nid)}
            )
            name = rows[0].get("name") if rows else None
        except Exception:
            name = None
        return {"name": str(name or nid), "path": _subject_path(nid, "notebooks")}

    return None


def _build_payload(
    event: JobEvent, job: Dict, subject: Optional[Dict[str, str]]
) -> Dict[str, Any]:
    label = str(job.get("name") or "job").replace("_", " ")
    completed = event == "completed"
    title = (
        f"{'✓' if completed else '✗'} {label} {'complete' if completed else 'failed'}"
    )

    lines = []
    if subject and subject.get("name"):
        lines.append(subject["name"])
    if not completed and job.get("error_message"):
        lines.append(str(job["error_message"]))
    if not lines:
        lines.append(label)

    payload: Dict[str, Any] = {
        "title": title,
        "message": "\n".join(lines),
        "priority": 3 if completed else 4,
    }

    base = os.getenv("OPEN_NOTEBOOK_BASE_URL", "").strip()
    if base and subject and subject.get("path"):
        payload["click"] = f"{base.rstrip('/')}{subject['path']}"
    return payload


async def _post(url: str, payload: Dict[str, Any], job_id: Any) -> None:
    """Deliver one ntfy message. Success is silent; failure logs once."""
    headers = {}
    token = os.getenv("OPEN_NOTEBOOK_NOTIFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(
            timeout=5.0, verify=httpx_verify_setting()
        ) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                logger.warning(
                    f"notify: ntfy returned {response.status_code} for {job_id}"
                )
    except Exception as e:
        logger.warning(f"notify: ntfy POST failed for {job_id}: {e}")


async def notify_job_event(event: JobEvent, job: Dict) -> None:
    """Log a job lifecycle event and, if configured, push it to ntfy."""
    reason = job.get("error_message")
    msg = f"job_event={event} id={job.get('id')} name={job.get('name')} app={job.get('app')} status={job.get('status')}"
    if reason:
        msg = f"{msg} reason={reason}"
    logger.info(msg)

    url = _notify_url()
    if not url:
        return

    try:
        subject = await _resolve_subject(_extract_args(job))
        payload = _build_payload(event, job, subject)
    except Exception as e:
        logger.warning(f"notify: could not build payload for {job.get('id')}: {e}")
        return

    try:
        task = asyncio.create_task(_post(url, payload, job.get("id")))
    except RuntimeError as e:  # no running loop — cannot fire-and-forget
        logger.warning(f"notify: no event loop to deliver {job.get('id')}: {e}")
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
