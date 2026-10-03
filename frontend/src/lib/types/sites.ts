export type SiteStatus = 'idle' | 'running' | 'completed' | 'failed'

export interface Site {
  id: string
  url: string
  name?: string | null
  restrict_to_origin: boolean
  follow_sitemap: boolean
  respect_robots_txt: boolean
  max_pages: number
  user_agent?: string | null
  status?: string | null
  last_run_at?: string | null
  last_run_duration?: number | null
  last_error?: string | null
  created: string
  updated: string
}

export interface SiteSummary {
  id: string
  url: string
  name?: string | null
  max_pages: number
  status?: string | null
  last_run_at?: string | null
  last_run_duration?: number | null
  last_error?: string | null
  created: string
  updated: string
  source_count: number
}

export interface SiteInput {
  url: string
  name?: string | null
  restrict_to_origin?: boolean
  follow_sitemap?: boolean
  respect_robots_txt?: boolean
  max_pages?: number
  user_agent?: string | null
}

export interface SiteUpdateInput {
  name?: string | null
  restrict_to_origin?: boolean
  follow_sitemap?: boolean
  respect_robots_txt?: boolean
  max_pages?: number
  user_agent?: string | null
}

export interface CrawlSiteRequest {
  notebook_ids?: string[]
  transformations?: string[]
  embed?: boolean
  max_pages?: number | null
}

export interface CrawlSiteResponse {
  job_id: string
  message: string
}

export interface SiteSource {
  id: string
  title?: string | null
  url?: string | null
  status?: string | null
  created: string
  updated: string
}

export const ACTIVE_SITE_STATUSES = ['running']