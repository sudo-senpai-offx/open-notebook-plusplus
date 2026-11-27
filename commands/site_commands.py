import asyncio
import time
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import Asset, Notebook, Source
from open_notebook.domain.site import Site
from open_notebook.exceptions import ConfigurationError
from open_notebook.scheduler.submit import submit_held
from open_notebook.utils.crawler import DEFAULT_USER_AGENT, crawl_site


class CrawlSiteInput(CommandInput):
    site_id: str
    notebook_ids: List[str]
    transformations: List[str]
    embed: bool
    max_pages: Optional[int] = None


class CrawlSiteOutput(CommandOutput):
    success: bool
    site_id: str
    pages_crawled: int
    sources_created: int
    processing_time: float
    error_message: Optional[str] = None


def _title_from_url(url: str) -> str:
    """Build a human-readable Source title from a discovered page URL."""
    parsed = urlparse(url)
    host = parsed.hostname or url
    if parsed.path and parsed.path.strip("/"):
        last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if last_segment:
            return f"{host} - {last_segment}"
    return host


# Bounded so a large crawl cannot fan out an unbounded number of database
# connections at once (each repo call opens its own SurrealDB connection).
INGEST_CONCURRENCY = 8

# Every page ingest creates a Source and RELATEs it to the same notebooks.
# SurrealDB aborts concurrent transactions that write the same record with a
# read/write conflict, so the create+relate section must be serialized
# process-wide even though the crawl discovery (network I/O) stays parallel.
INGEST_WRITE_LOCK = asyncio.Lock()


async def _ingest_page(
    page_url: str,
    site_id: str,
    notebook_ids: List[str],
    transformations: List[str],
    embed: bool,
    new_hash: str,
) -> bool:
    """Create or update a ``Source`` for one crawled page and enqueue ingest.

    If a Source already exists for this URL+site_id (re-crawl with changed
    content), its content_hash is updated and the job is re-submitted.
    Otherwise a new Source is created. Each page is handled in isolation so
    a single failure cannot abort the rest of the crawl.
    Returns ``True`` when the source was created/updated and enqueued.
    """
    try:
        # Check whether a Source already exists for this URL on this site.
        rows = await repo_query(
            "SELECT id FROM source WHERE site_id = $site_id AND asset.url = $url",
            {"site_id": ensure_record_id(site_id), "url": page_url},
        )
        if rows:
            # Re-crawl with changed content: update hash, re-submit.
            source_id = str(rows[0]["id"])
            await repo_query(
                "UPDATE $id MERGE $patch",
                {
                    "id": ensure_record_id(source_id),
                    "patch": {"content_hash": new_hash},
                },
            )
        else:
            async with INGEST_WRITE_LOCK:
                source = Source(
                    title=_title_from_url(page_url),
                    asset=Asset(url=page_url),
                    site_id=ensure_record_id(site_id),
                )
                await source.save()
                source_id = str(source.id)

                for notebook_id in notebook_ids:
                    await source.add_to_notebook(notebook_id, validate=False)

        submit_held(
            "open_notebook",
            "process_source",
            {
                "source_id": source_id,
                "content_state": {"url": page_url},
                "notebook_ids": notebook_ids,
                "transformations": transformations,
                "embed": embed,
            },
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to ingest page {page_url}: {e}")
        return False


@command(
    "crawl_site",
    app="open_notebook",
    retry={
        "max_attempts": 3,  # Per-page failures are already survived by the crawler
        "wait_strategy": "exponential_jitter",
        "wait_min": 1,
        "wait_max": 60,
        "stop_on": [
            ValueError,
            ConfigurationError,
        ],  # Don't retry validation/config errors
        "retry_log_level": "debug",
    },
)
async def crawl_site_command(input_data: CrawlSiteInput) -> CrawlSiteOutput:
    """
    Crawl a registered site and feed discovered pages into the ingest pipeline.

    The crawler only discovers URLs (utils/crawler.py). Each page becomes a
    ``Source`` linked via ``site_id``, attached to the requested notebooks, and
    then submitted to the existing ``process_source`` command so content-core
    handles extraction, chunking, transformations, and embedding.

    Re-crawling is content-aware: each page's HTML is hashed and compared
    against the stored ``content_hash``. Pages whose content hasn't changed are
    skipped; only new or changed pages are (re)processed. This fixes the
    previous behavior where re-crawls skipped all previously-seen URLs even
    when their content had changed (Failure #2).
    """
    start_time = time.time()
    site_id = input_data.site_id

    try:
        site = await Site.get(site_id)
        if site is None:
            raise ValueError(f"Site '{site_id}' not found")

        logger.info(
            f"Starting crawl for site {site_id} ({site.url}) "
            f"max_pages={site.max_pages if input_data.max_pages is None else input_data.max_pages} "
            f"sitemap={site.follow_sitemap} robots={site.respect_robots_txt}"
        )

        site.status = "running"
        site.last_error = None
        await site.save()

        max_pages = (
            input_data.max_pages if input_data.max_pages is not None else site.max_pages
        )

        # Get existing sources with content hashes for change detection.
        existing_rows = await repo_query(
            "SELECT id, asset.url, content_hash FROM source "
            "WHERE site_id = $site_id AND asset.url != NONE",
            {"site_id": ensure_record_id(site_id)},
        )
        existing_hashes: dict[str, str] = {}
        for row in existing_rows or []:
            asset = row.get("asset") or {}
            url = asset.get("url")
            ch = row.get("content_hash")
            if url and ch:
                existing_hashes[url] = ch

        # Crawl with content-hash awareness: returns url → new_hash (or None if unchanged).
        pages = await crawl_site(
            site.url,
            user_agent=site.user_agent or DEFAULT_USER_AGENT,
            max_pages=max_pages,
            restrict_to_origin=site.restrict_to_origin,
            respect_robots_txt=site.respect_robots_txt,
            follow_sitemap=site.follow_sitemap,
            existing_hashes=existing_hashes,
        )
        logger.info(f"Crawled {len(pages)} pages for site {site_id}")

        # Validate target notebooks once up front (a small, user-chosen set) so
        # the per-page relate loop does not re-fetch a full Notebook model for
        # every page. Invalid notebooks are skipped with a warning, mirroring the
        # per-page failure tolerance of the ingest loop.
        validated_notebooks: List[str] = []
        for notebook_id in input_data.notebook_ids:
            try:
                await Notebook.get(notebook_id)
                validated_notebooks.append(notebook_id)
            except Exception as e:
                logger.warning(f"Skipping invalid notebook {notebook_id}: {e}")

        semaphore = asyncio.Semaphore(INGEST_CONCURRENCY)

        async def _bounded(page_url: str, new_hash: Optional[str]) -> bool:
            async with semaphore:
                return await _ingest_page(
                    page_url,
                    site_id,
                    validated_notebooks,
                    input_data.transformations,
                    input_data.embed,
                    new_hash,
                )

        # Only submit pages that are new or changed (new_hash is not None).
        to_process = [
            (url, new_hash) for url, new_hash in pages.items() if new_hash is not None
        ]
        results = await asyncio.gather(
            *(_bounded(url, new_hash) for url, new_hash in to_process)
        )
        sources_created = sum(1 for ok in results if ok)

        processing_time = time.time() - start_time
        site.status = "completed"
        site.last_run_at = datetime.now(timezone.utc)
        site.last_run_duration = processing_time
        await site.save()

        logger.info(
            f"Finished crawling site {site_id}: {len(pages)} pages, "
            f"{sources_created} sources created in {processing_time:.2f}s"
        )
        return CrawlSiteOutput(
            success=True,
            site_id=site_id,
            pages_crawled=len(pages),
            sources_created=sources_created,
            processing_time=processing_time,
        )

    except ValueError as e:
        processing_time = time.time() - start_time
        logger.error(f"Crawl failed for site {site_id} (permanent): {e}")
        await _mark_failed(site_id, str(e))
        raise
    except Exception as e:
        processing_time = time.time() - start_time
        logger.debug(f"Transient error crawling site {site_id}: {e}")
        await _mark_failed(site_id, str(e))
        raise


async def _mark_failed(site_id: str, error_message: str) -> None:
    """Persist the failed status onto the site so the UI can surface it."""
    try:
        site = await Site.get(site_id)
        if site is None:
            return
        site.status = "failed"
        site.last_error = error_message
        site.last_run_at = datetime.now(timezone.utc)
        await site.save()
    except Exception as save_err:
        logger.error(f"Failed to mark site {site_id} as failed: {save_err}")
