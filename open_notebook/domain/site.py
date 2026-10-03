from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, List, Optional

from pydantic import ConfigDict, Field, field_validator

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.base import ObjectModel
from open_notebook.exceptions import DatabaseOperationError

if TYPE_CHECKING:
    from open_notebook.domain.notebook import Source


class Site(ObjectModel):
    """A registered website the user wants turned into sources.

    The model holds crawl configuration and lifecycle state; the actual crawl is
    executed by the `crawl_site` command (see commands/site_commands.py). Pages it
    discovers are stored as `Source` records linked via `source.site_id`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    table_name: ClassVar[str] = "site"

    url: str = Field(description="Seed URL the crawl starts from")
    name: Optional[str] = None
    restrict_to_origin: bool = True
    follow_sitemap: bool = True
    respect_robots_txt: bool = True
    max_pages: int = Field(default=50, ge=1, le=1000)
    user_agent: Optional[str] = None
    status: Optional[str] = "idle"
    last_run_at: Optional[datetime] = None
    last_run_duration: Optional[float] = None
    last_error: Optional[str] = None

    @field_validator("id", mode="before")
    @classmethod
    def parse_id(cls, value):
        """Parse id field to handle both string and RecordID inputs"""
        if value is None:
            return None
        return str(value) if value else None

    async def get_sources(self) -> List["Source"]:
        """Sources crawled for this site (bypassing notebook relationships)."""
        if not self.id:
            raise DatabaseOperationError(f"Saving site before crawling: {self.url}")
        try:
            result = await repo_query(
                "SELECT * FROM source WHERE site_id = $site_id ORDER BY updated DESC",
                {"site_id": ensure_record_id(self.id)},
            )
            from open_notebook.domain.notebook import Source

            return [Source(**row) for row in result] if result else []
        except Exception as e:
            raise DatabaseOperationError(f"Error fetching sources for site {self.id}: {e}") from e

    async def get_source_urls(self) -> set[str]:
        """Page URLs already ingested for this site, used to skip re-crawls."""
        if not self.id:
            raise DatabaseOperationError(f"Saving site before crawling source urls: {self.url}")
        try:
            rows = await repo_query(
                "SELECT asset.url FROM source WHERE site_id = $site_id AND asset.url != NONE",
                {"site_id": ensure_record_id(self.id)},
            )
            urls = set()
            for row in rows or []:
                asset = row.get("asset") or {}
                url = asset.get("url")
                if url:
                    urls.add(url)
            return urls
        except Exception as e:
            raise DatabaseOperationError(f"Error fetching source urls for site {self.id}: {e}") from e