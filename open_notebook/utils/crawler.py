"""
Site crawling utilities (ADR-008).

The crawler only *discovers* URLs — it is deliberately minimal. Ingestion of each
page is delegated to the existing ``process_source`` command (content-core), which
the ``crawl_site`` worker command calls after creating the ``Source`` records.

Security note: every outbound HTTP request goes through
``prepare_pinned_http_target`` so DNS is resolved once to a vetted IP and the
connection cannot be re-routed to a metadata endpoint mid-request. This class of
crawler must never call ``httpx.get()`` directly on a user-supplied hostname.
"""

import asyncio
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from open_notebook.utils.url_validation import (
    PinnedHttpTarget,
    prepare_pinned_http_target,
)

DEFAULT_USER_AGENT = "OpenNotebookBot/1.0 (+site crawler)"

# DNS re-resolution is the expensive part of each fetch; hosts are few on any
# crawl (same-origin bias), so cache the pinned netloc + Host/SNI overrides per
# scheme+host. Only the *resolution facts* are cached — the request URL itself
# is rebuilt per call so a target pinned for one path is never reused for a
# different path (the full URL is encoded in PinnedHttpTarget.url).
_pinned_target_cache: Dict[tuple, Tuple[str, Optional[str], Optional[str]]] = {}

# Bound concurrent page fetches so large frontier waves cannot fan out an
# unbounded number of connections through the shared client at once.
FETCH_CONCURRENCY = 8


async def _pinned_target(url: str) -> PinnedHttpTarget:
    """Return a DNS-pinned http target for ``url``, cached per scheme+host.

    The pinned netloc and Host/SNI overrides are resolved once per host and
    cached; the returned ``PinnedHttpTarget`` is then rebuilt with ``url``'s
    own path so consecutive requests to the same host with different paths do
    not silently replay the first request's URL.
    """
    parsed = urlparse(url)
    key = (parsed.scheme, parsed.netloc)
    entry = _pinned_target_cache.get(key)
    if entry is None:
        resolved = await prepare_pinned_http_target(url, "site_crawler")
        entry = (
            urlparse(resolved.url).netloc,
            resolved.headers.get("Host"),
            resolved.extensions.get("sni_hostname"),
        )
        _pinned_target_cache[key] = entry
    pinned_netloc, host_header, sni_hostname = entry
    pinned_url = urlunparse(
        (
            parsed.scheme,
            pinned_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    headers: Dict[str, str] = {}
    if host_header:
        headers["Host"] = host_header
    extensions: Dict[str, Any] = {}
    if sni_hostname:
        extensions["sni_hostname"] = sni_hostname
    return PinnedHttpTarget(url=pinned_url, headers=headers, extensions=extensions)


_crawl_client: Optional[httpx.AsyncClient] = None


def _get_crawl_client() -> httpx.AsyncClient:
    """Lazily create (and reuse) the crawler's shared HTTP client.

    One long-lived client is used instead of one-per-request so connections are
    pooled across the crawl; creating a fresh ``AsyncClient`` per request opens
    a new TCP connection each time, which is wasteful and can drop request
    delivery when many connections churn rapidly.
    """
    global _crawl_client
    if _crawl_client is None:
        _crawl_client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
        )
    return _crawl_client


async def _pinned_get(
    url: str, user_agent: str, max_redirects: int = 5
) -> Optional[httpx.Response]:
    """Fetch ``url`` over httpx, pinning DNS on every redirect hop.

    Returns ``None`` on transport/validation errors (the crawl treats a failed
    page as "not discovered" rather than aborting the whole site).
    """
    current = url
    response = None
    for _ in range(max_redirects + 1):
        try:
            target = await _pinned_target(current)
            headers = dict(target.headers)
            headers["User-Agent"] = user_agent
            headers.setdefault("Accept", "text/html,application/xhtml+xml,text/*;q=0.9")
            response = await _get_crawl_client().get(
                target.url,
                headers=headers,
                extensions=target.extensions,
            )
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.debug(f"Crawl fetch failed for {current}: {e}")
            return None

        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        if not location:
            return None
        current = urljoin(url, location)
        if urlparse(current).scheme not in ("http", "https"):
            return None
    return None


async def fetch_robots_txt(
    seed_url: str, user_agent: str = DEFAULT_USER_AGENT
) -> Optional[RobotFileParser]:
    """Fetch and parse ``/robots.txt`` for the origin of ``seed_url``.

    Returns ``None`` when the site has no robots.txt (or it is unreachable),
    which callers treat as "everything allowed".
    """
    parsed = urlparse(seed_url)
    robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    response = await _pinned_get(robots_url, user_agent)
    if response is None or response.status_code != 200:
        return None
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(response.text.splitlines())
    return parser


def _extract_sitemap_locs(xml_text: str) -> List[str]:
    """Extract all ``<loc>`` values from a sitemap/sitemapindex body (XML)."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    locs: List[str] = []
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] == "loc" and elem.text:
            locs.append(elem.text.strip())
    return locs


async def _fetch_sitemap_locs(
    sitemap_url: str,
    user_agent: str,
    collect_into: List[str],
    budget: int,
    depth: int = 0,
) -> None:
    """Recursively gather page URLs from sitemap.xml, following sitemapindexes.

    ``budget`` bounds total pages collected so a malicious/huge sitemap cannot
    explode the crawl. ``depth`` bounds index nesting (a real source, but
    defensive against cyclic sitemap refs).
    """
    if depth > 3 or len(collect_into) >= budget:
        return
    response = await _pinned_get(sitemap_url, user_agent)
    if response is None or response.status_code != 200:
        return
    locs = _extract_sitemap_locs(response.text)

    root_tag = ""
    try:
        root_tag = ElementTree.fromstring(response.text).tag.rsplit("}", 1)[-1]
    except ElementTree.ParseError:
        return
    remaining = budget - len(collect_into)
    if root_tag == "sitemapindex":
        for loc in locs[: max(0, min(remaining, 50))]:
            await _fetch_sitemap_locs(loc, user_agent, collect_into, budget, depth + 1)
    else:
        collect_into.extend(locs[:remaining])


async def fetch_sitemap_urls(
    seed_url: str, user_agent: str = DEFAULT_USER_AGENT, budget: int = 1000
) -> List[str]:
    """Gather page URLs advertised by the site's ``/sitemap.xml``."""
    parsed = urlparse(seed_url)
    sitemap_url = urlunparse((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "", ""))
    locs: List[str] = []
    await _fetch_sitemap_locs(sitemap_url, user_agent, locs, budget)
    return locs


def _normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop fragment, keep query. Used for dedupe."""
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def _same_origin(url_a: str, url_b: str) -> bool:
    pa = urlparse(url_a)
    pb = urlparse(url_b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def extract_links(html: str, page_url: str) -> Set[str]:
    """Return absolute, http(s) ``<a href>`` URLs from ``html``.

    Honors a ``<base href>`` override and strips fragments/duplicates.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base")
    if base_tag and base_tag.get("href"):
        page_url = urljoin(page_url, str(base_tag["href"]).strip())

    links: Set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        links.add(_normalize_url(absolute))
    return links


async def crawl_site(
    seed_url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    max_pages: int = 50,
    restrict_to_origin: bool = True,
    respect_robots_txt: bool = True,
    follow_sitemap: bool = True,
    existing_urls: Optional[Set[str]] = None,
) -> List[str]:
    """Breadth-first crawl ``seed_url`` and return discovered page URLs.

    Robots.txt is honored when ``respect_robots_txt`` is set (default), sitemap
    URLs seed the frontier when ``follow_sitemap`` is set, and discovered links
    outside the seed origin are dropped when ``restrict_to_origin`` is set.
    Pages already present in ``existing_urls`` are never returned, so re-crawls
    are incremental.
    """
    seed = _normalize_url(seed_url)
    existing = existing_urls or set()

    robots = await fetch_robots_txt(seed, user_agent) if respect_robots_txt else None

    def _allowed(url: str) -> bool:
        if urlparse(url).scheme not in ("http", "https"):
            return False
        if restrict_to_origin and not _same_origin(url, seed):
            return False
        if robots is not None and not robots.can_fetch(user_agent, url):
            return False
        if url in existing:
            return False
        return True

    seen: Set[str] = set()
    frontier: deque = deque()

    candidates: List[str] = [seed]
    if follow_sitemap:
        candidates.extend(
            await fetch_sitemap_urls(seed, user_agent, budget=max_pages * 4)
        )
    for url in candidates:
        url = _normalize_url(url)
        if url not in seen and _allowed(url):
            seen.add(url)
            frontier.append(url)

    discovered: List[str] = []
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _fetch_bounded(url: str) -> Optional[httpx.Response]:
        """Fetch one frontier URL, bounded by ``FETCH_CONCURRENCY``."""
        async with semaphore:
            return await _pinned_get(url, user_agent)

    while frontier and len(discovered) < max_pages:
        batch: List[str] = []
        while frontier and len(batch) < FETCH_CONCURRENCY:
            url = frontier.popleft()
            if _allowed(url):
                batch.append(url)
        if not batch:
            break

        responses = await asyncio.gather(*(_fetch_bounded(url) for url in batch))
        for url, response in zip(batch, responses):
            if response is None or response.status_code != 200:
                continue
            content_type = (response.headers.get("content-type", "") or "").lower()
            if "text/html" not in content_type and "xhtml" not in content_type:
                continue

            discovered.append(url)
            if len(discovered) >= max_pages:
                break

            for link in extract_links(response.text, url):
                if link not in seen and _allowed(link):
                    seen.add(link)
                    frontier.append(link)
        if len(discovered) >= max_pages:
            break

    return discovered
