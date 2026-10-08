from typing import Dict, Literal

from loguru import logger

JobEvent = Literal["completed", "failed"]


async def notify_job_event(event: JobEvent, job: Dict) -> None:
    """Log-only job lifecycle event notification (D1 seam)."""
    reason = job.get("error_message")
    msg = f"job_event={event} id={job.get('id')} name={job.get('name')} app={job.get('app')} status={job.get('status')}"
    if reason:
        msg = f"{msg} reason={reason}"
    logger.info(msg)
