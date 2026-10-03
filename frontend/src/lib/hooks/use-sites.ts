import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { sitesApi } from '@/lib/api/sites'
import { QUERY_KEYS } from '@/lib/api/query-client'
import { useToast } from '@/lib/hooks/use-toast'
import { useTranslation } from '@/lib/hooks/use-translation'
import { getApiErrorKey } from '@/lib/utils/error-handler'
import {
  ACTIVE_SITE_STATUSES,
  CrawlSiteRequest,
  SiteInput,
  SiteSummary,
  SiteUpdateInput,
} from '@/lib/types/sites'

function hasActiveSites(sites: SiteSummary[]) {
  return sites.some((site) => {
    const status = site.status ?? 'idle'
    return ACTIVE_SITE_STATUSES.includes(status)
  })
}

export function useSites(options?: { autoRefresh?: boolean }) {
  const { autoRefresh = true } = options ?? {}

  const query = useQuery({
    queryKey: QUERY_KEYS.sites,
    queryFn: sitesApi.listSites,
    refetchInterval: (current) => {
      if (!autoRefresh) {
        return false
      }

      const data = current.state.data as SiteSummary[] | undefined
      if (!data || data.length === 0) {
        return false
      }

      return hasActiveSites(data) ? 15_000 : false
    },
  })

  const sites = useMemo(() => query.data ?? [], [query.data])

  const active = useMemo(() => hasActiveSites(sites), [sites])

  return {
    ...query,
    sites,
    hasActiveSites: active,
  }
}

export function useCreateSite() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (payload: SiteInput) => sitesApi.createSite(payload),
    onSuccess: async (data) => {
      await queryClient.refetchQueries({ queryKey: QUERY_KEYS.sites })
      toast({
        title: t('sites.siteCreated'),
        description: t('sites.siteCreatedDesc', { name: data.name || data.url }),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('sites.failedToCreateSite'),
        description: getApiErrorKey(error, t('common.error')),
        variant: 'destructive',
      })
    },
  })
}

export function useUpdateSite() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: ({ siteId, payload }: { siteId: string; payload: SiteUpdateInput }) =>
      sitesApi.updateSite(siteId, payload),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: QUERY_KEYS.sites })
      toast({
        title: t('sites.siteUpdated'),
        description: t('sites.siteUpdatedDesc', { name: data.name || data.url }),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('sites.failedToUpdateSite'),
        description: getApiErrorKey(error, t('common.error')),
        variant: 'destructive',
      })
    },
  })
}

export function useDeleteSite() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: (siteId: string) => sitesApi.deleteSite(siteId),
    onSuccess: async (_data, siteId) => {
      await queryClient.invalidateQueries({ queryKey: QUERY_KEYS.sites })
      await queryClient.removeQueries({ queryKey: QUERY_KEYS.site(siteId) })
      await queryClient.removeQueries({ queryKey: QUERY_KEYS.siteSources(siteId) })
      toast({
        title: t('sites.siteDeleted'),
        description: t('sites.siteDeletedDesc'),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('sites.failedToDeleteSite'),
        description: getApiErrorKey(error, t('common.error')),
        variant: 'destructive',
      })
    },
  })
}

export function useCrawlSite() {
  const queryClient = useQueryClient()
  const { toast } = useToast()
  const { t } = useTranslation()

  return useMutation({
    mutationFn: ({ siteId, payload }: { siteId: string; payload?: CrawlSiteRequest }) =>
      sitesApi.crawlSite(siteId, payload),
    onSuccess: async () => {
      await queryClient.refetchQueries({ queryKey: QUERY_KEYS.sites })
      toast({
        title: t('sites.crawlStarted'),
        description: t('sites.crawlStartedDesc'),
      })
    },
    onError: (error: unknown) => {
      toast({
        title: t('sites.crawlFailed'),
        description: getApiErrorKey(error, t('sites.alreadyRunning')),
        variant: 'destructive',
      })
    },
  })
}