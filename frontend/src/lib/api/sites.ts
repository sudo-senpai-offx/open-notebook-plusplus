import apiClient from './client'
import {
  CrawlSiteRequest,
  CrawlSiteResponse,
  Site,
  SiteInput,
  SiteSource,
  SiteSummary,
  SiteUpdateInput,
} from '@/lib/types/sites'

export const sitesApi = {
  listSites: async () => {
    const response = await apiClient.get<SiteSummary[]>('/sites')
    return response.data
  },

  getSite: async (siteId: string) => {
    const response = await apiClient.get<Site>(`/sites/${siteId}`)
    return response.data
  },

  createSite: async (payload: SiteInput) => {
    const response = await apiClient.post<Site>('/sites', payload)
    return response.data
  },

  updateSite: async (siteId: string, payload: SiteUpdateInput) => {
    const response = await apiClient.put<Site>(`/sites/${siteId}`, payload)
    return response.data
  },

  deleteSite: async (siteId: string) => {
    await apiClient.delete(`/sites/${siteId}`)
  },

  crawlSite: async (siteId: string, payload?: CrawlSiteRequest) => {
    const response = await apiClient.post<CrawlSiteResponse>(
      `/sites/${siteId}/crawl`,
      payload ?? {}
    )
    return response.data
  },

  listSiteSources: async (siteId: string) => {
    const response = await apiClient.get<SiteSource[]>(`/sites/${siteId}/sources`)
    return response.data
  },
}