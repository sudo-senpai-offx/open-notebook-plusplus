# ADR-008: Site crawling feeds the existing process_source pipeline

- **Status**: Accepted
- **Date**: 2026-09
- **Related**: site-crawling feature (next after podcast ingestion)

## Context

Users need to ingest a whole website into a notebook, not paste links one by one.
The naive approach a crawler is built from — fetch a page, extract its content —
is already a solved problem in this codebase: `process_source` (via content-core)
extracts text from a `Source` whose `asset.url` is set, and notebooks already
consume `Source` records. The open loop is *discovery*: which pages on a site are
worth turning into sources.

Two constraints shape the design:

1. **SSRF safety is mandatory.** The URL validator's `prepare_pinned_http_target`
   re-resolves and pins every outbound fetch to a DNS-vetted IP (and blocks
   link-local / AWS metadata ranges). A crawler that ignores that pinning
   re-introduces the hole the validator exists to close.
2. **Extraction must not be duplicated.** If the crawler also extracted page
   text, the ingest code path would fork into two competing implementations that
   drift apart (one in the crawler, one in content-core).

## Decision

**The crawler only discovers URLs; ingestion is delegated to the existing
`process_source` pipeline.**

- A `crawl_site` worker command walks a site (robots.txt, optional sitemap,
  then breadth-first link crawling bounded by `max_pages`). For each page it
  creates a `Source` record with `asset.url` and `site_id`, attaches it to the
  requested notebooks, and submits `process_source` with `content_state.url` —
  the exact same job a manually pasted link produces. No extraction code lives
  in the crawler.
- **Every** HTTP fetch — robots.txt, sitemap, and page bodies — goes through
  `prepare_pinned_http_target`. No direct `httpx.get()` exists in the crawler.
- A `site` table stores crawl configuration and state (seed URL, name,
  `restrict_to_origin`, `follow_sitemap`, `respect_robots_txt`, `max_pages`,
  `user_agent`, status/`last_run_at`/`last_error`). Migration 24 adds it as
  `SCHEMAFULL` and adds a `site_id` link field on `source` so sources are
  attributable to (and filterable by) their site.
- Crawling runs asynchronously: the `/sites/{id}/crawl` API endpoint validates
  the request then submits the `crawl_site` command and returns immediately.
  The command updates `site.status` through its lifecycle.
- Existing sources are not re-ingested: a page already present for the same
  `site_id` is skipped, so re-crawling is incremental and safe.
- New dependency: `beautifulsoup4` for link extraction only (parsing `<a href>`
  and `<base href>`). The rest is stdlib (`urllib.robotparser`,
  `urllib.parse`, `xml.etree`).

## Alternatives considered

- **Crawler extracts content directly.** Rejected: forks the extraction code
  path, bypasses content-core's file/URL handling, and would have to reimplement
  its chunking/embedding side effects. Reusing `process_source` keeps one ingest
  pipeline.
- **A persistence layer / repository object for sites.** Rejected: the codebase
  deliberately has no repository abstraction — `ObjectModel.save()` hits
  SurrealDB directly. `Site.ObjectModel` follows that convention.
- **Reusing an existing scraping dependency (scrapy / trafilatura).** Rejected:
  scrapy is a full spider framework (heavy, opinionated, hard to reach through
  one pinned target); the whole crawl is a bounded BFS over pinned targets, which
  the small crawler util covers. BeautifulSoup alone is enough.

## Consequences

- Crawl workers stay on the same retry/observability rails as every other
  `surreal-commands` job (`retry` policy, status baked into `site`).
- Robots/sitemap rules are applied at discovery time; pages later processed by
  `process_source` are ordinary sources and behave identically to pasted links.
- `site_id` is a soft link (`option<record<site>>`): deleting a site nulls the
  link on its sources rather than cascading, so sources survive a site deletion.
- Per ADR-006, this ships as migration 24 with its `_down` counterpart, reviewed
  together with the crawler code.