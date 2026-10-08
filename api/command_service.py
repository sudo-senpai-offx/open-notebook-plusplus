from typing import Any, Dict, List, Optional

from loguru import logger
from surreal_commands import submit_command

from open_notebook.database.repository import ensure_record_id, repo_query


class CommandService:
    """Generic service layer for command operations"""

    @staticmethod
    async def submit_command_job(
        module_name: str,  # Actually app_name for surreal-commands
        command_name: str,
        command_args: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit a generic command job for background processing"""
        try:
            # Ensure command modules are imported before submitting
            # This is needed because submit_command validates against local registry
            try:
                import commands.podcast_commands  # noqa: F401
                import commands.site_commands  # noqa: F401
            except ImportError as import_err:
                logger.error(f"Failed to import command modules: {import_err}")
                raise ValueError("Command modules not available")

            # surreal-commands expects: submit_command(app_name, command_name, args)
            cmd_id = submit_command(
                module_name,  # This is actually the app name (e.g., "open_notebook")
                command_name,  # Command name (e.g., "generate_podcast")
                command_args,  # Input data
            )
            # Convert RecordID to string if needed
            if not cmd_id:
                raise ValueError("Failed to get cmd_id from submit_command")
            cmd_id_str = str(cmd_id)
            logger.info(
                f"Submitted command job: {cmd_id_str} for {module_name}.{command_name}"
            )
            return cmd_id_str

        except Exception as e:
            logger.error(f"Failed to submit command job: {e}")
            raise

    @staticmethod
    async def get_command_status(job_id: str) -> Dict[str, Any]:
        """Get status of any command job (direct row read)."""
        try:
            jid = str(job_id)
            if ":" not in jid:
                jid = f"command:{jid}"
            rows = await repo_query("SELECT * FROM $id", {"id": ensure_record_id(jid)})
            if not rows:
                raise ValueError(f"Job {job_id} not found")
            row = rows[0]
            has_progress = any(
                row.get(k) is not None for k in ("progress", "stage", "heartbeat_at")
            )
            progress = (
                {
                    "percent": row.get("progress"),
                    "stage": row.get("stage"),
                    "heartbeat_at": row.get("heartbeat_at"),
                }
                if has_progress
                else None
            )
            return {
                "job_id": str(row.get("id")),
                "status": row.get("status", "unknown"),
                "result": row.get("result"),
                "error_message": row.get("error_message"),
                "created": str(row.get("created")) if row.get("created") else None,
                "updated": str(row.get("updated")) if row.get("updated") else None,
                "progress": progress,
            }
        except Exception as e:
            logger.error(f"Failed to get command status: {e}")
            raise

    @staticmethod
    async def list_command_jobs(
        module_filter: Optional[str] = None,
        command_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List command jobs with optional filtering"""
        # This will be implemented with proper SurrealDB queries
        # For now, return empty list as this is foundation phase
        return []

    @staticmethod
    async def cancel_command_job(job_id: str) -> bool:
        """Cancel a running command job"""
        try:
            jid = str(job_id)
            if ":" not in jid:
                jid = f"command:{jid}"
            rows = await repo_query(
                "SELECT status FROM $id", {"id": ensure_record_id(jid)}
            )
            if not rows:
                return False
            status = rows[0].get("status")
            if status not in ("new", "running"):
                return False
            from datetime import datetime, timezone

            await repo_query(
                "UPDATE $id MERGE $patch",
                {
                    "id": ensure_record_id(jid),
                    "patch": {
                        "cancel_requested": True,
                        "cancel_requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
            return True
        except Exception as e:
            logger.error(f"Failed to cancel command job: {e}")
            raise
