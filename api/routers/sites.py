from typing import Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import ValidationError

from api.command_service import CommandService
from api.models import (
    CrawlSiteRequest,
    CrawlSiteResponse,
    SiteCreate,
    SiteListResponse,
    SiteResponse,
    SiteSourceResponse,
    SiteUpdate,
)
from commands.site_commands import CrawlSiteInput
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.site import Site
from open_notebook.exceptions import InvalidInputError, NotFoundError, OpenNotebookError

router = APIRouter()


def _validate_http_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidInputError("URL must be a valid http(s) URL")
    return url.strip()


def _to_site_response(
    site: Site,
    source_count: Optional[int] = None,
) -> SiteResponse:
    return SiteResponse(
        id=str(site.id),
        url=site.url,
        name=site.name,
        restrict_to_origin=site.restrict_to_origin,
        follow_sitemap=site.follow_sitemap,
        respect_robots_txt=site.respect_robots_txt,
        max_pages=site.max_pages,
        user_agent=site.user_agent,
        status=site.status,
        last_run_at=site.last_run_at.isoformat() if site.last_run_at else None,
        last_run_duration=site.last_run_duration,
        last_error=site.last_error,
        created=str(site.created),
        updated=str(site.updated),
    )


async def _site_source_counts() -> Dict[str, int]:
    """Map each site record id -> number of crawled sources (field link, not graph edge)."""
    counts: Dict[str, int] = {}
    try:
        result = await repo_query(
            "SELECT site_id, count() AS cnt FROM source WHERE site_id IS NOT NONE GROUP BY site_id",
            {},
        )
        for row in result or []:
            site_record = str(row.get("site_id", ""))
            if not site_record:
                continue
            counts[site_record] = counts.get(site_record, 0) + int(row.get("cnt") or 0)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"Failed to compute site source counts: {e}")
    return counts


@router.get("/sites", response_model=List[SiteListResponse])
async def list_sites():
    """List all registered sites with their crawl source counts."""
    try:
        sites = await Site.get_all(order_by="updated desc")
        counts = await _site_source_counts()
        return [
            SiteListResponse(
                id=str(site.id),
                url=site.url,
                name=site.name,
                max_pages=site.max_pages,
                status=site.status,
                last_run_at=site.last_run_at.isoformat()
                if site.last_run_at
                else None,
                last_run_duration=site.last_run_duration,
                last_error=site.last_error,
                created=str(site.created),
                updated=str(site.updated),
                source_count=counts.get(str(site.id), 0),
            )
            for site in sites
        ]
    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error listing sites: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error listing sites: {str(e)}")


@router.post("/sites", response_model=SiteResponse)
async def create_site(site_data: SiteCreate):
    """Register a new site to crawl."""
    try:
        _validate_http_url(site_data.url)
        name = site_data.name or urlparse(site_data.url).hostname or site_data.url
        site = Site(
            url=site_data.url.strip(),
            name=name,
            restrict_to_origin=site_data.restrict_to_origin,
            follow_sitemap=site_data.follow_sitemap,
            respect_robots_txt=site_data.respect_robots_txt,
            max_pages=site_data.max_pages,
            user_agent=site_data.user_agent,
        )
        await site.save()
        return _to_site_response(site)
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating site: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating site: {str(e)}")


@router.get("/sites/{site_id}", response_model=SiteResponse)
async def get_site(site_id: str):
    """Get a single site by ID."""
    try:
        site = await Site.get(site_id)
        return _to_site_response(site)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching site {site_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching site: {str(e)}")


@router.put("/sites/{site_id}", response_model=SiteResponse)
async def update_site(site_id: str, site_update: SiteUpdate):
    """Update site crawl configuration."""
    try:
        site = await Site.get(site_id)
        if site_update.name is not None:
            site.name = site_update.name
        if site_update.restrict_to_origin is not None:
            site.restrict_to_origin = site_update.restrict_to_origin
        if site_update.follow_sitemap is not None:
            site.follow_sitemap = site_update.follow_sitemap
        if site_update.respect_robots_txt is not None:
            site.respect_robots_txt = site_update.respect_robots_txt
        if site_update.max_pages is not None:
            site.max_pages = site_update.max_pages
        if site_update.user_agent is not None:
            site.user_agent = site_update.user_agent
        await site.save()
        return _to_site_response(site)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating site {site_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating site: {str(e)}")


@router.delete("/sites/{site_id}", response_model=Dict[str, str])
async def delete_site(site_id: str):
    """Delete a site and detach (but keep) its crawled sources."""
    try:
        site = await Site.get(site_id)
        # Detach sources first - they survive the site deletion (soft link).
        await repo_query(
            "UPDATE source SET site_id = NONE WHERE site_id = $site_id",
            {"site_id": ensure_record_id(site_id)},
        )
        await site.delete()
        return {"status": "deleted", "id": str(site_id)}
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting site {site_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting site: {str(e)}")


@router.post("/sites/{site_id}/crawl", response_model=CrawlSiteResponse)
async def crawl_site(site_id: str, crawl_request: CrawlSiteRequest):
    """Submit an async crawl job for a site."""
    try:
        site = await Site.get(site_id)
        if site.status == "running":
            raise HTTPException(
                status_code=409, detail="Site is already being crawled"
            )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    try:
        command_input = CrawlSiteInput(
            site_id=str(site.id),
            notebook_ids=crawl_request.notebook_ids,
            transformations=crawl_request.transformations or [],
            embed=crawl_request.embed,
            max_pages=crawl_request.max_pages,
        )
        job_id = await CommandService.submit_command_job(
            "open_notebook",
            "crawl_site",
            command_input.model_dump(),
        )
        logger.info(f"Submitted crawl job {job_id} for site {site_id}")
        return CrawlSiteResponse(job_id=job_id)
    except Exception as e:
        logger.error(f"Failed to submit crawl job for site {site_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to submit crawl job: {str(e)}"
        )


@router.get("/sites/{site_id}/sources", response_model=List[SiteSourceResponse])
async def list_site_sources(site_id: str):
    """List sources crawled for a site."""
    try:
        site = await Site.get(site_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    try:
        sources = await site.get_sources()
        return [
            SiteSourceResponse(
                id=str(source.id),
                title=source.title,
                url=source.asset.url if source.asset else None,
                status=source.status if hasattr(source, "status") else None,
                created=str(source.created),
                updated=str(source.updated),
            )
            for source in sources
        ]
    except Exception as e:
        logger.error(f"Error listing sources for site {site_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error listing sources: {str(e)}")