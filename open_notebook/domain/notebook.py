import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterable, List, Literal, Optional, Union
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from surrealdb import RecordID

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.base import ObjectModel
from open_notebook.exceptions import (
    DatabaseOperationError,
    InvalidInputError,
    NotFoundError,
)
from open_notebook.scheduler.submit import submit_held
from open_notebook.storage.blobs import resolve_blob_path, unlink_if_unreferenced


class Notebook(ObjectModel):
    table_name: ClassVar[str] = "notebook"
    name: str
    description: str
    archived: Optional[bool] = False
    last_viewed_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v):
        if not v.strip():
            raise InvalidInputError("Notebook name cannot be empty")
        return v

    async def get_sources(self, include_full_text: bool = False) -> List["Source"]:
        try:
            source_projection = "" if include_full_text else " omit source.full_text"
            srcs = await repo_query(
                f"""
                select *{source_projection} from (
                select in as source from reference where out=$id
                fetch source
            ) order by source.updated desc
            """,
                {"id": ensure_record_id(self.id)},
            )
            return [Source(**src["source"]) for src in srcs] if srcs else []
        except Exception as e:
            logger.error(f"Error fetching sources for notebook {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def get_notes(self, include_content: bool = False) -> List["Note"]:
        try:
            note_projection = (
                " omit note.embedding"
                if include_content
                else " omit note.content, note.embedding"
            )
            srcs = await repo_query(
                f"""
            select *{note_projection} from (
                select in as note from artifact where out=$id
                fetch note
            ) order by note.updated desc
            """,
                {"id": ensure_record_id(self.id)},
            )
            return [Note(**src["note"]) for src in srcs] if srcs else []
        except Exception as e:
            logger.error(f"Error fetching notes for notebook {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def get_context(self) -> str:
        """
        Build long-form notebook context for podcast and LLM workflows.

        Normal list retrieval omits large source/note bodies, so this method uses
        opt-in full-content fetches and formats only substantive context blocks.
        """
        sources = await self.get_sources(include_full_text=True)
        notes = await self.get_notes(include_content=True)
        context_blocks = []

        insights_by_source = await SourceInsight.get_for_sources(
            [source.id for source in sources if source.id]
        )
        for source in sources:
            source_context = await source.get_context(
                context_size="long",
                insights=insights_by_source.get(source.id or "", []),
            )
            if isinstance(source_context, dict):
                title = source_context.get("title") or source.title or "Untitled source"
                full_text = source_context.get("full_text")
                insights = source_context.get("insights") or []

                content_parts = []
                if full_text:
                    content_parts.append(str(full_text))

                insight_lines = []
                for insight in insights:
                    if not isinstance(insight, dict):
                        continue

                    insight_content = insight.get("content")
                    if not insight_content:
                        continue

                    insight_type = insight.get("insight_type") or "Insight"
                    insight_lines.append(f"- {insight_type}: {insight_content}")

                if insight_lines:
                    content_parts.append("Insights:\n" + "\n".join(insight_lines))

                content = "\n\n".join(content_parts).strip()
            else:
                title = source.title or "Untitled source"
                content = str(source_context).strip()

            if content:
                context_blocks.append(f"## Source: {title}\n\n{content}")

        for note in notes:
            note_context = note.get_context(context_size="long")
            if isinstance(note_context, dict):
                title = note_context.get("title") or note.title or "Untitled note"
                content = note_context.get("content")
                content = str(content).strip() if content else ""
            else:
                title = note.title or "Untitled note"
                content = str(note_context).strip()

            if content:
                context_blocks.append(f"## Note: {title}\n\n{content}")

        return "\n\n".join(context_blocks)

    async def get_chat_sessions(self) -> List["ChatSession"]:
        try:
            srcs = await repo_query(
                """
                select * from (
                    select
                    <- chat_session as chat_session
                    from refers_to
                    where out=$id
                    fetch chat_session
                )
                order by chat_session.updated desc
            """,
                {"id": ensure_record_id(self.id)},
            )
            return (
                [ChatSession(**src["chat_session"][0]) for src in srcs] if srcs else []
            )
        except Exception as e:
            logger.error(
                f"Error fetching chat sessions for notebook {self.id}: {str(e)}"
            )
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def get_delete_preview(self) -> Dict[str, Any]:
        """
        Get counts of items that would be affected by deleting this notebook.

        Returns a dict with:
        - note_count: Number of notes that will be deleted
        - exclusive_source_count: Sources only in this notebook (can be deleted)
        - shared_source_count: Sources in other notebooks (will be unlinked only)
        """
        try:
            notebook_id = ensure_record_id(self.id)

            # Count notes
            note_result = await repo_query(
                "SELECT count() as count FROM artifact WHERE out = $notebook_id GROUP ALL",
                {"notebook_id": notebook_id},
            )
            note_count = note_result[0]["count"] if note_result else 0

            # Get sources with count of references to OTHER notebooks
            # If assigned_others = 0, source is exclusive to this notebook
            # If assigned_others > 0, source is shared with other notebooks
            source_counts = await repo_query(
                """
                SELECT
                    id,
                    count(->reference[WHERE out != $notebook_id].out) as assigned_others
                FROM (SELECT VALUE <-reference.in AS sources FROM $notebook_id)[0]
                """,
                {"notebook_id": notebook_id},
            )

            exclusive_count = 0
            shared_count = 0
            for src in source_counts:
                if src.get("assigned_others", 0) == 0:
                    exclusive_count += 1
                else:
                    shared_count += 1

            return {
                "note_count": note_count,
                "exclusive_source_count": exclusive_count,
                "shared_source_count": shared_count,
            }
        except Exception as e:
            logger.error(f"Error getting delete preview for notebook {self.id}: {e}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def delete(self, delete_exclusive_sources: bool = False) -> Dict[str, int]:
        """
        Delete notebook with cascade deletion of notes and optional source deletion.

        Args:
            delete_exclusive_sources: If True, also delete sources that belong
                                     only to this notebook. Default is False.

        Returns:
            Dict with counts: deleted_notes, deleted_sources, unlinked_sources,
            deleted_chat_sessions
        """
        if self.id is None:
            raise InvalidInputError("Cannot delete notebook without an ID")

        try:
            notebook_id = ensure_record_id(self.id)
            deleted_notes = 0
            deleted_sources = 0
            unlinked_sources = 0
            deleted_chat_sessions = 0

            # 1. Get and delete all notes linked to this notebook
            notes = await self.get_notes()
            for note in notes:
                await note.delete()
                deleted_notes += 1
            logger.info(f"Deleted {deleted_notes} notes for notebook {self.id}")

            # Delete artifact relationships
            await repo_query(
                "DELETE artifact WHERE out = $notebook_id",
                {"notebook_id": notebook_id},
            )

            # 2. Handle sources
            if delete_exclusive_sources:
                # Find sources with count of references to OTHER notebooks
                # If assigned_others = 0, source is exclusive to this notebook
                source_counts = await repo_query(
                    """
                    SELECT
                        id,
                        count(->reference[WHERE out != $notebook_id].out) as assigned_others
                    FROM (SELECT VALUE <-reference.in AS sources FROM $notebook_id)[0]
                    """,
                    {"notebook_id": notebook_id},
                )

                for src in source_counts:
                    source_id = src.get("id")
                    if source_id and src.get("assigned_others", 0) == 0:
                        # Exclusive source - delete it
                        try:
                            source = await Source.get(str(source_id))
                            await source.delete()
                            deleted_sources += 1
                        except Exception as e:
                            logger.warning(
                                f"Failed to delete exclusive source {source_id}: {e}"
                            )
                    else:
                        unlinked_sources += 1
            else:
                # Just count sources that will be unlinked
                source_result = await repo_query(
                    "SELECT count() as count FROM reference WHERE out = $notebook_id GROUP ALL",
                    {"notebook_id": notebook_id},
                )
                unlinked_sources = source_result[0]["count"] if source_result else 0

            # Delete reference relationships (unlink all sources)
            await repo_query(
                "DELETE reference WHERE out = $notebook_id",
                {"notebook_id": notebook_id},
            )
            logger.info(
                f"Unlinked {unlinked_sources} sources, deleted {deleted_sources} "
                f"exclusive sources for notebook {self.id}"
            )

            # 3. Delete chat sessions linked to this notebook
            chat_sessions = await self.get_chat_sessions()
            for chat_session in chat_sessions:
                await chat_session.delete()
                deleted_chat_sessions += 1
            logger.info(
                f"Deleted {deleted_chat_sessions} chat sessions for notebook {self.id}"
            )

            # 4. Delete the notebook record itself
            await super().delete()
            logger.info(f"Deleted notebook {self.id}")

            return {
                "deleted_notes": deleted_notes,
                "deleted_sources": deleted_sources,
                "unlinked_sources": unlinked_sources,
                "deleted_chat_sessions": deleted_chat_sessions,
            }

        except Exception as e:
            logger.error(f"Error deleting notebook {self.id}: {e}")
            logger.exception(e)
            raise DatabaseOperationError(f"Failed to delete notebook: {e}")


class Asset(BaseModel):
    """Where a source's content lives.

    ``sha256`` is the content address: the same bytes always produce the
    same value, on every device, which is what lets a sync manifest name
    a file without trusting the sender's path. ``filename`` preserves the
    name the user recognises - the stored blob is named by hash, so
    without it every download would be ``a3f9….pdf``.

    ``file_path`` remains authoritative locally because extraction, the
    PDF text-layer probe and the download route all read it directly.
    """

    file_path: Optional[str] = None
    url: Optional[str] = None
    sha256: Optional[str] = None
    filename: Optional[str] = None
    size: Optional[int] = None


class SourceEmbedding(ObjectModel):
    table_name: ClassVar[str] = "source_embedding"
    content: str

    async def get_source(self) -> "Source":
        try:
            src = await repo_query(
                """
            select source.* from $id fetch source
            """,
                {"id": ensure_record_id(self.id)},
            )
            return Source(**src[0]["source"])
        except Exception as e:
            logger.error(f"Error fetching source for embedding {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)


class SourceInsight(ObjectModel):
    table_name: ClassVar[str] = "source_insight"
    insight_type: str
    content: str

    @classmethod
    async def get_for_sources(
        cls, source_ids: List[str]
    ) -> Dict[str, List["SourceInsight"]]:
        """
        Batch-fetch insights for many sources in a single query.

        Building notebook/chat context otherwise calls get_insights() once
        per source - fine for one source, but O(n) round trips (each paying
        its own connection setup - no pooling in the repository layer) when
        a caller loops over every source in a notebook.
        """
        grouped: Dict[str, List[SourceInsight]] = {sid: [] for sid in source_ids if sid}
        if not grouped:
            return grouped
        try:
            result = await repo_query(
                "SELECT * FROM source_insight WHERE source IN $source_ids",
                {"source_ids": [ensure_record_id(sid) for sid in grouped]},
            )
        except Exception as e:
            logger.error(f"Error batch-fetching insights for sources: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch insights for sources")
        for row in result:
            key = str(row.get("source"))
            grouped.setdefault(key, []).append(cls(**row))
        return grouped

    async def get_source(self) -> "Source":
        try:
            src = await repo_query(
                """
            select source.* from $id fetch source
            """,
                {"id": ensure_record_id(self.id)},
            )
            return Source(**src[0]["source"])
        except Exception as e:
            logger.error(f"Error fetching source for insight {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def save_as_note(self, notebook_id: Optional[str] = None) -> Any:
        source = await self.get_source()
        note = Note(
            title=f"{self.insight_type} from source {source.title}",
            content=self.content,
        )
        await note.save()
        if notebook_id:
            await note.add_to_notebook(notebook_id)
        return note


async def _auto_delete_files_enabled() -> bool:
    """Whether the operator asked for uploaded files to be removed on delete.

    Fails closed toward *keeping* the file: if the settings record cannot
    be read, an unreadable preference must not become a destructive one.
    """
    from open_notebook.domain.content_settings import ContentSettings

    try:
        settings = await ContentSettings.get_instance()
    except Exception as exc:  # noqa: BLE001 - a missing preference is not fatal
        logger.warning(
            f"Could not read auto_delete_files, keeping the file: "
            f"{type(exc).__name__}: {exc}"
        )
        return False
    return (settings.auto_delete_files or "no") == "yes"


class Source(ObjectModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    table_name: ClassVar[str] = "source"
    asset: Optional[Asset] = None
    title: Optional[str] = None
    topics: Optional[List[str]] = Field(default_factory=list)
    full_text: Optional[str] = None
    last_viewed_at: Optional[datetime] = None
    command: Optional[Union[str, RecordID]] = Field(
        default=None, description="Link to surreal-commands processing job"
    )
    site_id: Optional[Union[str, RecordID]] = Field(
        default=None, description="Link to the site this source was crawled from"
    )

    @field_validator("command", mode="before")
    @classmethod
    def parse_command(cls, value):
        """Parse command field to ensure RecordID format"""
        if isinstance(value, str) and value:
            return ensure_record_id(value)
        return value

    @property
    def local_file(self) -> Optional[str]:
        """The source's file on *this* device, or None when absent.

        A source that arrived by sync has a content hash but no local
        path — the path on the device that created it means nothing
        here. So the path is derived from the hash on every access, and
        stays None until a blob transfer brings the bytes. Callers get a
        truthful answer without needing to know whether this source was
        ingested locally or imported.
        """
        if not self.asset:
            return None
        if self.asset.file_path:
            return self.asset.file_path
        return resolve_blob_path(self.asset.sha256, self.asset.filename)

    @property
    def has_local_file(self) -> bool:
        """True when the bytes for this source are present on this device."""
        path = self.local_file
        return bool(path) and os.path.exists(path)

    async def link_local_file(self) -> bool:
        """Point ``asset.file_path`` at the local blob when one exists.

        Called after a blob transfer so the stored path stops being
        redundant with the hash. Never overwrites a path that already
        resolves — an uploaded source's path is as authoritative as its
        hash.
        """
        if not self.asset or not self.asset.sha256:
            return False
        if self.asset.file_path and os.path.exists(self.asset.file_path):
            return True
        resolved = resolve_blob_path(self.asset.sha256, self.asset.filename)
        if not resolved:
            return False
        self.asset.file_path = resolved
        return True

    @field_validator("site_id", mode="before")
    @classmethod
    def parse_site_id(cls, value):
        """Parse site_id field to ensure RecordID format"""
        if isinstance(value, str) and value:
            return ensure_record_id(value)
        return value

    @field_validator("id", mode="before")
    @classmethod
    def parse_id(cls, value):
        """Parse id field to handle both string and RecordID inputs"""
        if value is None:
            return None
        if isinstance(value, RecordID):
            return str(value)
        return str(value) if value else None

    async def get_status(self) -> Optional[str]:
        """Get the processing status of the associated command"""
        if not self.command:
            return None

        try:
            from surreal_commands import get_command_status

            status = await get_command_status(str(self.command))
            return status.status if status else "unknown"
        except Exception as e:
            logger.warning(f"Failed to get command status for {self.command}: {e}")
            return "unknown"

    async def get_processing_progress(self) -> Optional[Dict[str, Any]]:
        """Get detailed processing information for the associated command"""
        if not self.command:
            return None

        try:
            cid = str(self.command)
            if ":" not in cid:
                cid = f"command:{cid}"
            rows = await repo_query(
                "SELECT status, result, error_message, progress, stage, heartbeat_at, cancel_requested FROM $id",
                {"id": ensure_record_id(cid)},
            )
            if not rows:
                return None
            row = rows[0]
            result = row.get("result") if isinstance(row.get("result"), dict) else None
            execution_metadata = result.get("execution_metadata", {}) if result else {}
            return {
                "status": row.get("status"),
                "started_at": execution_metadata.get("started_at"),
                "completed_at": execution_metadata.get("completed_at"),
                "error": row.get("error_message"),
                "result": result,
                "progress": row.get("progress"),
                "stage": row.get("stage"),
                "heartbeat_at": row.get("heartbeat_at"),
                "cancel_requested": row.get("cancel_requested"),
            }
        except Exception as e:
            logger.warning(f"Failed to get command progress for {self.command}: {e}")
            return None

    async def get_context(
        self,
        context_size: Literal["short", "long"] = "short",
        insights: Optional[List["SourceInsight"]] = None,
    ) -> Dict[str, Any]:
        # Callers looping over many sources can batch-fetch insights up front
        # via SourceInsight.get_for_sources() and pass them in here, instead
        # of paying a separate query per source.
        insight_objects = (
            insights if insights is not None else await self.get_insights()
        )
        insights = [insight.model_dump() for insight in insight_objects]
        if context_size == "long":
            return dict(
                id=self.id,
                title=self.title,
                insights=insights,
                full_text=self.full_text,
            )
        else:
            return dict(id=self.id, title=self.title, insights=insights)

    async def get_embedded_chunks(self) -> int:
        try:
            result = await repo_query(
                """
                select count() as chunks from source_embedding where source=$id GROUP ALL
                """,
                {"id": ensure_record_id(self.id)},
            )
            if len(result) == 0:
                return 0
            return result[0]["chunks"]
        except Exception as e:
            logger.error(f"Error fetching chunks count for source {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError(f"Failed to count chunks for source: {str(e)}")

    async def get_insights(self) -> List[SourceInsight]:
        try:
            result = await repo_query(
                """
                SELECT * FROM source_insight WHERE source=$id
                """,
                {"id": ensure_record_id(self.id)},
            )
            return [SourceInsight(**insight) for insight in result]
        except Exception as e:
            logger.error(f"Error fetching insights for source {self.id}: {str(e)}")
            logger.exception(e)
            raise DatabaseOperationError("Failed to fetch insights for source")

    async def add_to_notebook(self, notebook_id: str, validate: bool = True) -> Any:
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        if validate:
            # SurrealDB record links do NOT prevent dangling references, so verify
            # the notebook exists eagerly. Callers that already validated the set
            # (e.g. the site crawl worker) skip this to avoid a full model fetch
            # once per relate.
            await Notebook.get(notebook_id)  # raises NotFoundError if invalid/missing
        return await self.relate("reference", notebook_id)

    async def vectorize(self) -> str:
        """
        Submit vectorization as a background job using the embed_source command.

        This method leverages the job-based architecture to prevent HTTP connection
        pool exhaustion when processing large documents. The embed_source command:
        1. Detects content type from file path
        2. Chunks text using content-type aware splitter
        3. Generates all embeddings in batches
        4. Bulk inserts source_embedding records

        Returns:
            str: The command/job ID that can be used to track progress via the commands API

        Raises:
            ValueError: If source has no text to vectorize
            DatabaseOperationError: If job submission fails
        """
        logger.info(f"Submitting embed_source job for source {self.id}")

        try:
            if not self.full_text or not self.full_text.strip():
                raise ValueError(f"Source {self.id} has no text to vectorize")

            # Submit the embed_source command
            command_id = submit_held(
                "open_notebook",
                "embed_source",
                {"source_id": str(self.id)},
            )

            command_id_str = str(command_id)
            logger.info(
                f"Embed source job submitted for source {self.id}: "
                f"command_id={command_id_str}"
            )

            return command_id_str

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Failed to submit embed_source job for source {self.id}: {e}")
            logger.exception(e)
            raise DatabaseOperationError(e)

    async def add_insight(self, insight_type: str, content: str) -> str:
        """
        Submit insight creation as an async command (fire-and-forget).

        Submits a create_insight command that handles database operations with
        automatic retry logic for transaction conflicts. The command also submits
        an embed_insight command for async embedding.

        This method returns immediately after submitting the command - it does NOT
        wait for the insight to be created. Use this for batch operations where
        throughput is more important than immediate confirmation.

        Args:
            insight_type: Type/category of the insight
            content: The insight content text

        Returns:
            command_id for optional tracking

        Raises:
            InvalidInputError: If insight_type or content is empty
            DatabaseOperationError: If submitting the command fails. Matches
                vectorize()'s contract - callers (transformation.py, source.py)
                run inside surreal-commands jobs whose outer exception
                handling already retries transient failures, so a swallowed
                submission failure here previously meant a transformation
                could report success while the insight was silently never
                persisted.
        """
        if not insight_type or not content:
            raise InvalidInputError("Insight type and content must be provided")

        try:
            # Submit create_insight command (fire-and-forget)
            # Command handles retries internally for transaction conflicts
            command_id = submit_held(
                "open_notebook",
                "create_insight",
                {
                    "source_id": str(self.id),
                    "insight_type": insight_type,
                    "content": content,
                },
            )
            logger.info(
                f"Submitted create_insight command {command_id} for source {self.id} "
                f"(type={insight_type})"
            )
            return str(command_id)

        except Exception as e:
            logger.exception(
                f"Error submitting create_insight for source {self.id}: {e}"
            )
            raise DatabaseOperationError(e)

    def _prepare_save_data(self) -> dict:
        """Override to ensure command field is always RecordID format for database"""
        data = super()._prepare_save_data()

        # Ensure command field is RecordID format if not None
        if data.get("command") is not None:
            data["command"] = ensure_record_id(data["command"])

        return data

    async def delete(self, keep_blob: Optional[bool] = None) -> bool:
        """Delete source and clean up associated file, embeddings, and insights.

        Deleting a source means *forget it*, not *destroy it*. The record
        goes; the bytes stay unless the operator has asked otherwise.

        `keep_blob` defaults to the `auto_delete_files` setting, which
        itself now defaults to "no". Two reasons for that default:

          * a delete syncs. When the other device's tombstone lands here,
            this device may be the last one holding the file, and
            unlinking would turn someone else's delete into permanent
            data loss;
          * the user is reminded before deleting, so a deletion is a
            decision rather than an accident - and a decision stays
            reversible for as long as the file is on disk.

        The setting still exists and still works; set it to "yes" to
        reclaim disk on delete. The cost of the default is that
        `data/blobs` grows and is never reclaimed by this path, which
        seems the right trade for lecture material.

        Passing keep_blob explicitly overrides the setting, which is how
        a tombstone application can guarantee it never destroys bytes
        regardless of local preference.
        """
        if keep_blob is None:
            # Inverted deliberately: the setting says whether to *remove*
            # the file, this argument says whether to *keep* it, and the
            # setting's default of "no" has to mean "keep".
            keep_blob = not await _auto_delete_files_enabled()

        # Clean up the backing file, but only if nothing else still needs
        # it. Content-addressed storage means two sources can share one
        # blob, and the unconditional unlink this replaces would have
        # deleted bytes the surviving source still reads from.
        if self.asset and self.asset.file_path and not keep_blob:
            file_path = Path(self.asset.file_path)
            sha256 = self.asset.sha256
            try:
                if sha256:
                    removed = await unlink_if_unreferenced(
                        str(file_path), sha256, str(self.id)
                    )
                    if removed:
                        logger.info(f"Deleted blob for source {self.id}: {file_path}")
                    elif file_path.exists():
                        logger.info(
                            f"Kept blob for source {self.id}: {file_path} "
                            "is still referenced by another source"
                        )
                elif file_path.exists():
                    # Pre-blob source: no hash, so nothing can be sharing
                    # the path. Old behaviour, kept for existing rows.
                    os.unlink(file_path)
                    logger.info(f"Deleted file for source {self.id}: {file_path}")
                else:
                    logger.debug(
                        f"File {file_path} not found for source {self.id}, skipping cleanup"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to delete file {file_path} for source {self.id}: {e}. "
                    "Continuing with database deletion."
                )

        # Delete associated embeddings and insights to prevent orphaned records
        try:
            source_id = ensure_record_id(self.id)
            await repo_query(
                "DELETE source_embedding WHERE source = $source_id",
                {"source_id": source_id},
            )
            await repo_query(
                "DELETE source_insight WHERE source = $source_id",
                {"source_id": source_id},
            )
            logger.debug(f"Deleted embeddings and insights for source {self.id}")
        except Exception as e:
            logger.warning(
                f"Failed to delete embeddings/insights for source {self.id}: {e}. "
                "Continuing with source deletion."
            )

        # Call parent delete to remove database record
        return await super().delete()


class Note(ObjectModel):
    table_name: ClassVar[str] = "note"
    title: Optional[str] = None
    note_type: Optional[Literal["human", "ai"]] = None
    content: Optional[str] = None

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v):
        if v is not None and not v.strip():
            raise InvalidInputError("Note content cannot be empty")
        return v

    async def save(self) -> Optional[str]:
        """
        Save the note and submit embedding command.

        Overrides ObjectModel.save() to submit an async embed_note command
        after saving, instead of inline embedding.

        Returns:
            Optional[str]: The command_id if embedding was submitted, None
                otherwise (either no content to embed, or submission failed)
        """
        # Call parent save (without embedding)
        await super().save()

        # Submit embedding command (fire-and-forget) if note has content.
        # Unlike Source.vectorize()/add_insight() (explicit, dedicated calls
        # whose whole point is the submission), this runs automatically
        # inside save() - the note itself is already durably saved above,
        # so a submission hiccup here shouldn't fail an otherwise-successful
        # save with a 500. Best-effort: log and move on.
        if self.id and self.content and self.content.strip():
            try:
                command_id = submit_held(
                    "open_notebook",
                    "embed_note",
                    {"note_id": str(self.id)},
                )
                logger.debug(f"Submitted embed_note command {command_id} for {self.id}")
                return command_id
            except Exception as e:
                logger.error(f"Failed to submit embed_note command for {self.id}: {e}")
                return None

        return None

    async def add_to_notebook(self, notebook_id: str) -> Any:
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        await Notebook.get(notebook_id)  # raises NotFoundError if invalid/missing
        return await self.relate("artifact", notebook_id)

    def get_context(
        self, context_size: Literal["short", "long"] = "short"
    ) -> Dict[str, Any]:
        if context_size == "long":
            return dict(id=self.id, title=self.title, content=self.content)
        else:
            return dict(
                id=self.id,
                title=self.title,
                content=self.content[:100] if self.content else None,
            )


class ChatSession(ObjectModel):
    table_name: ClassVar[str] = "chat_session"
    nullable_fields: ClassVar[set[str]] = {"model_override"}
    title: Optional[str] = None
    model_override: Optional[str] = None

    async def relate_to_notebook(self, notebook_id: str) -> Any:
        if not notebook_id:
            raise InvalidInputError("Notebook ID must be provided")
        return await self.relate("refers_to", notebook_id)

    async def relate_to_source(self, source_id: str) -> Any:
        if not source_id:
            raise InvalidInputError("Source ID must be provided")
        return await self.relate("refers_to", source_id)


async def resolve_notebook_scope(notebook_ids: List[str]) -> List[str]:
    """Validate a notebook scope before it reaches text_search / vector_search.

    Every id must be a well-formed `notebook:<key>` id naming an existing
    notebook: a typo would otherwise silently return an empty result set that
    is indistinguishable from "no matches". Existence is checked with one query
    for the whole list. Returns the ids unchanged (empty list = whole knowledge
    base). Raises InvalidInputError for malformed ids, NotFoundError for
    unknown notebooks.
    """
    if not notebook_ids:
        return []

    # Only notebook ids are accepted: a source or note id would fail the typed
    # record<notebook> parameter inside SurrealDB instead of returning cleanly,
    # and "notebook:" with an empty key would blow up in RecordID.parse.
    record_ids: List[RecordID] = []
    invalid: List[str] = []
    for nb_id in notebook_ids:
        if not nb_id.startswith("notebook:") or not nb_id[len("notebook:") :]:
            invalid.append(nb_id)
            continue
        try:
            record_ids.append(ensure_record_id(nb_id))
        except Exception:
            invalid.append(nb_id)
    if invalid:
        raise InvalidInputError(f"Invalid notebook id(s): {', '.join(invalid)}")

    try:
        rows = await repo_query(
            "SELECT id FROM notebook WHERE id IN $ids", {"ids": record_ids}
        )
    except Exception as e:
        # Same contract as text_search / vector_search: log the raw driver
        # error here, surface a typed error to the API layer.
        logger.error(f"Error resolving notebook scope: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError("Failed to resolve notebook scope")
    # The driver returns ids as RecordID objects (unhashable, and never equal
    # to the request strings), so normalize to strings before comparing.
    found = {str(row["id"]) for row in rows}
    missing = [nb_id for nb_id in notebook_ids if nb_id not in found]
    if missing:
        raise NotFoundError(f"Notebook(s) not found: {', '.join(missing)}")
    return notebook_ids


def _scope_record_ids(notebook_ids: Optional[List[str]]) -> Optional[List[RecordID]]:
    """Normalize a notebook scope for fn::text_search / fn::vector_search.

    Returns None for an empty scope (global search) so the SurrealQL functions
    take their unfiltered path, otherwise the ids as RecordIDs.
    """
    if not notebook_ids:
        return None
    return [ensure_record_id(nb_id) for nb_id in notebook_ids]


async def text_search(
    keyword: str,
    results: int,
    source: bool = True,
    note: bool = True,
    notebook_ids: Optional[List[str]] = None,
):
    if not keyword:
        raise InvalidInputError("Search keyword cannot be empty")
    try:
        search_results = await repo_query(
            """
            select *
            from fn::text_search($keyword, $results, $source, $note, $notebook_ids)
            """,
            {
                "keyword": keyword,
                "results": results,
                "source": source,
                "note": note,
                "notebook_ids": _scope_record_ids(notebook_ids),
            },
        )
        return search_results
    except RuntimeError as e:
        # SurrealDB's search::highlight can compute a byte position that exceeds the
        # stored string length on large or multi-byte chunks, aborting the whole query
        # ("position overflow"). Fall back to vector search so the user still gets
        # results instead of a 500. See issue #648.
        if "position overflow" in str(e):
            logger.warning(
                f"Highlight position overflow, falling back to vector search: {str(e)}"
            )
            try:
                return await vector_search(
                    keyword, results, source, note, notebook_ids=notebook_ids
                )
            except Exception as ve:
                # Both search paths failed (e.g. no embedding model configured).
                # Surface the failure instead of returning [] — an empty list would
                # be indistinguishable from a legitimate "no matches" and mask a
                # total search outage from callers.
                logger.error(f"Vector search fallback also failed: {str(ve)}")
                logger.exception(ve)
                raise DatabaseOperationError(ve)
        logger.error(f"Error performing text search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)
    except Exception as e:
        logger.error(f"Error performing text search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)


async def vector_search(
    keyword: str,
    results: int,
    source: bool = True,
    note: bool = True,
    minimum_score=0.2,
    notebook_ids: Optional[List[str]] = None,
    _timings: Optional[Dict[str, Optional[float]]] = None,
):
    """Vector search over embedded chunks.

    ``_timings`` is an optional side-channel (A1 step 0): when a dict is
    passed, the embed / retrieve / assemble phases are recorded in ms in
    place. Callers that omit it (notably the text->vector fallback inside
    text_search()) are completely unaffected.
    """
    if not keyword:
        raise InvalidInputError("Search keyword cannot be empty")
    try:
        from open_notebook.utils.embedding import generate_embedding
        from open_notebook.utils.embedding_prefix import EmbeddingRole

        # Use unified embedding function (handles chunking if query is very long)
        #
        # role=QUERY: this is the only place in the codebase embedding a
        # *query*; every other call embeds stored content. Without the role an
        # E5-family model would embed this as a passage and retrieve
        # measurably worse - which is invisible, because it still returns
        # results, just worse ones.
        _t0 = time.perf_counter()
        embed = await generate_embedding(keyword, role=EmbeddingRole.QUERY)
        _t1 = time.perf_counter()
        search_results = await repo_query(
            """
            SELECT * FROM fn::vector_search($embed, $results, $source, $note, $minimum_score, $notebook_ids);
            """,
            {
                "embed": embed,
                "results": results,
                "source": source,
                "note": note,
                "minimum_score": minimum_score,
                "notebook_ids": _scope_record_ids(notebook_ids),
            },
        )
        _t2 = time.perf_counter()
        # SurrealDB fn::vector_search declares ORDER BY similarity DESC, but the
        # SELECT * FROM fn::... wrapper can still return rows out of rank order.
        # Enforce descending similarity (stable by id for ties) before returning.
        ranked = sorted(
            search_results or [],
            key=lambda item: (
                -float(item.get("similarity") or 0.0),
                str(item.get("id") or ""),
            ),
        )
        _t3 = time.perf_counter()
        if _timings is not None:
            _timings["embed_ms"] = (_t1 - _t0) * 1000.0
            _timings["db_ms"] = (_t2 - _t1) * 1000.0
            _timings["assemble_ms"] = (_t3 - _t2) * 1000.0
        return ranked
    except Exception as e:
        logger.error(f"Error performing vector search: {str(e)}")
        logger.exception(e)
        raise DatabaseOperationError(e)


class ChatMessage(ObjectModel):
    """One turn of a conversation, stored where it can be merged.

    A transcript used to exist only inside the LangGraph checkpoint, which
    is opaque and unmergeable - so a session could not cross devices at
    all. This is the durable copy. The checkpoint stays as a resume cache
    for long conversations; this is what syncs.

    The record id is the merge key. It is minted once, when the message
    is written, and carried unchanged across devices, so two devices
    appending to the same session produce a union rather than a conflict.

    `seq` is for display order only. Two devices appending independently
    will hand out overlapping sequence numbers - that is expected, and
    ordering falls back to `created` then to id.
    """

    table_name: ClassVar[str] = "chat_message"
    nullable_fields: ClassVar[set[str]] = set()

    session: Optional[Union[str, RecordID]] = None
    seq: int = 0
    role: str = "human"
    content: str = ""

    @field_validator("session", mode="before")
    @classmethod
    def parse_session(cls, value):
        return ensure_record_id(value) if value else value

    @field_validator("role", mode="before")
    @classmethod
    def _normalise_role(cls, value: str) -> str:
        """Map LangChain's vocabulary onto the three the table asserts.

        A value outside this set is rejected by the schema, so a message
        type LangChain invents later cannot become a write that fails
        mid-turn.
        """
        mapping = {
            "human": "human",
            "ai": "ai",
            "system": "system",
        }
        return mapping.get(str(value), "system")

    async def transcript(self) -> List["ChatMessage"]:
        """Every message in this session, in the order they happened.

        Returns empty for a message with no session rather than raising.
        The schema makes `session` optional, so a partially-loaded row is
        representable; failing the read over it would turn a cosmetic gap
        into a 500 on a page the user is trying to read.
        """
        if not self.session:
            return []
        rows = await repo_query(
            "SELECT * FROM chat_message WHERE session = $sid ORDER BY seq ASC, created ASC",
            {"sid": ensure_record_id(str(self.session))},
        )
        return [ChatMessage(**row) for row in (rows or [])]


async def persist_chat_messages(
    session_id: str,
    messages: Iterable[Any],
) -> int:
    """Write messages that are not stored yet. Returns how many were added.

    Idempotent by record id, so it is safe to call on every turn and on
    every read: a conversation opened on a device that predates this
    table backfills itself the first time it is loaded, and a message
    that arrived by sync is not written twice.

    The checkpoint is left alone. It stays the resume cache; this only
    makes the transcript durable and mergeable.
    """
    existing = await repo_query(
        "SELECT VALUE id FROM chat_message WHERE session = $sid",
        {"sid": ensure_record_id(session_id)},
    )
    # Compared as full record ids. Matching the bare key against the
    # stored `chat_message:<key>` never succeeds, so every turn re-wrote
    # the whole transcript and the conversation doubled each time.
    have = {str(r) for r in (existing or [])}
    added = 0

    for index, message in enumerate(messages or []):
        raw_id = getattr(message, "id", None)
        # The positional fallback used by the response serialiser is not
        # stable across devices - it is index-based - so it is replaced
        # with a minted id. Anything already carrying a real one keeps it,
        # which is what makes a sync replay idempotent.
        key = (
            str(raw_id)
            if raw_id and not str(raw_id).startswith("msg_")
            else str(uuid4())
        )
        record_id = f"chat_message:{key}"
        if record_id in have:
            continue
        have.add(record_id)

        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        if not content.strip():
            continue

        await repo_query(
            """
            CREATE $id CONTENT {
                session: $sid, seq: $seq, role: $role,
                content: $content, updated: time::now()
            }
            """,
            {
                "id": ensure_record_id(record_id),
                "sid": ensure_record_id(session_id),
                "seq": index,
                "role": getattr(message, "type", "human"),
                "content": content,
            },
        )
        added += 1
    return added
