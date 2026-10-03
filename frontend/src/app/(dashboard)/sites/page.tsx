'use client'

import { useCallback, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Globe,
  Loader2,
  Plus,
  RefreshCcw,
  Trash2,
  Zap,
} from 'lucide-react'

import { AppShell } from '@/components/layout/AppShell'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { ConfirmDialog } from '@/components/common/ConfirmDialog'
import { Separator } from '@/components/ui/separator'
import { useSites, useCreateSite, useDeleteSite, useCrawlSite } from '@/lib/hooks/use-sites'
import { useTranslation } from '@/lib/hooks/use-translation'
import type { SiteSummary } from '@/lib/types/sites'

function getStatusVariant(status?: string | null): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (status === 'running') return 'default'
  if (status === 'completed') return 'secondary'
  if (status === 'failed') return 'destructive'
  return 'outline'
}

function siteStatusLabel(status: string | null | undefined, t: (key: string) => string) {
  if (status === 'running') return t('sites.statusRunning')
  if (status === 'completed') return t('sites.statusCompleted')
  if (status === 'failed') return t('sites.statusFailed')
  return t('sites.statusIdle')
}

export default function SitesPage() {
  const { t } = useTranslation()
  const { sites, isLoading, isError, refetch, isFetching } = useSites()
  const createSite = useCreateSite()
  const deleteSite = useDeleteSite()
  const crawlSite = useCrawlSite()

  const [createOpen, setCreateOpen] = useState(false)
  const [siteToDelete, setSiteToDelete] = useState<SiteSummary | null>(null)
  const [siteToCrawl, setSiteToCrawl] = useState<SiteSummary | null>(null)

  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [maxPages, setMaxPages] = useState('50')

  const handleRefresh = useCallback(() => {
    void refetch()
  }, [refetch])

  const resetForm = useCallback(() => {
    setUrl('')
    setName('')
    setMaxPages('50')
  }, [])

  const handleCreate = useCallback(async () => {
    const parsedMaxPages = Number(maxPages)
    createSite.mutate({
      url: url.trim(),
      name: name.trim() || null,
      max_pages: Number.isFinite(parsedMaxPages) && parsedMaxPages > 0 ? parsedMaxPages : 50,
    })
  }, [createSite, url, name, maxPages])

  const handleDelete = useCallback(async () => {
    if (!siteToDelete) return
    await deleteSite.mutateAsync(siteToDelete.id)
    setSiteToDelete(null)
  }, [deleteSite, siteToDelete])

  const handleCrawl = useCallback(async () => {
    if (!siteToCrawl) return
    await crawlSite.mutateAsync({ siteId: siteToCrawl.id })
    setSiteToCrawl(null)
  }, [crawlSite, siteToCrawl])

  const emptyState = !isLoading && sites.length === 0

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="px-6 py-6 space-y-6">
          <header className="flex flex-wrap items-center justify-between gap-3">
            <div className="space-y-1">
              <h1 className="font-display text-2xl font-bold tracking-tight">{t('sites.listTitle')}</h1>
              <p className="text-muted-foreground">
                {t('sites.listDesc')}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Button onClick={() => setCreateOpen(true)}>
                <Plus className="mr-2 h-4 w-4" />
                {t('sites.newSite')}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleRefresh}
                disabled={isFetching}
              >
                {isFetching ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCcw className="mr-2 h-4 w-4" />
                )}
                {t('common.refresh')}
              </Button>
            </div>
          </header>

          {isError ? (
            <Alert className="bg-destructive-tint text-destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>{t('sites.failedToLoad')}</AlertTitle>
              <AlertDescription>
                {t('sites.failedToLoadDesc')}
              </AlertDescription>
            </Alert>
          ) : null}

          {emptyState ? (
            <Card className="border-dashed">
              <CardContent className="flex flex-col items-center justify-center gap-3 py-16 text-center">
                <Globe className="h-8 w-8 text-muted-foreground" />
                <div className="space-y-1">
                  <CardTitle>{t('sites.noSitesYet')}</CardTitle>
                  <CardDescription>{t('sites.addFirstSite')}</CardDescription>
                </div>
                <Button onClick={() => setCreateOpen(true)}>
                  <Plus className="mr-2 h-4 w-4" />
                  {t('sites.newSite')}
                </Button>
              </CardContent>
            </Card>
          ) : null}

          {!emptyState && sites.length > 0 ? (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {sites.map((site) => {
                const siteName = site.name || site.url
                const running = site.status === 'running'
                return (
                  <Card key={site.id} className="flex flex-col">
                    <CardHeader className="space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <CardTitle className="truncate text-base">{siteName}</CardTitle>
                        <Badge variant={getStatusVariant(site.status)}>
                          {site.status === 'completed' ? (
                            <CheckCircle2 className="mr-1 h-3 w-3" />
                          ) : site.status === 'failed' ? (
                            <AlertCircle className="mr-1 h-3 w-3" />
                          ) : site.status === 'running' ? (
                            <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                          ) : null}
                          {siteStatusLabel(site.status, t)}
                        </Badge>
                      </div>
                      <CardDescription className="truncate">{site.url}</CardDescription>
                    </CardHeader>
                    <CardContent className="flex flex-1 flex-col gap-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant="outline" className="font-medium">
                          {t('sites.sources')}
                          <span className="ml-1.5 font-mono">{site.source_count}</span>
                        </Badge>
                        <Badge variant="outline" className="font-medium">
                          {t('sites.maxPagesLabel')}
                          <span className="ml-1.5 font-mono">{site.max_pages}</span>
                        </Badge>
                      </div>

                      <p className="text-xs text-muted-foreground">
                        {site.last_run_at
                          ? t('sites.lastRunAt', { time: site.last_run_at })
                          : t('sites.neverRun')}
                        {site.last_run_duration != null
                          ? ` · ${t('sites.durationSeconds', { seconds: String(Math.round(site.last_run_duration)) })}`
                          : ''}
                      </p>

                      {site.last_error ? (
                        <p className="line-clamp-2 text-xs text-destructive">
                          {t('sites.lastError')}: {site.last_error}
                        </p>
                      ) : null}

                      <Separator className="my-1" />

                      <div className="flex items-center gap-2">
                        <Button
                          variant="secondary"
                          size="sm"
                          className="flex-1"
                          onClick={() => setSiteToCrawl(site)}
                          disabled={running || crawlSite.isPending}
                        >
                          {running ? (
                            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          ) : (
                            <Zap className="mr-2 h-4 w-4" />
                          )}
                          {running ? t('sites.crawling') : t('sites.crawl')}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setSiteToDelete(site)}
                          disabled={running}
                          aria-label={t('sites.deleteSiteLabel')}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </CardContent>
                  </Card>
                )
              })}
            </div>
          ) : null}

          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{t('sites.addSite')}</DialogTitle>
                <DialogDescription>{t('sites.addSiteDesc')}</DialogDescription>
              </DialogHeader>
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="site-url">{t('sites.urlLabel')}</Label>
                  <Input
                    id="site-url"
                    placeholder={t('sites.urlPlaceholder')}
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="site-name">{t('sites.nameLabel')}</Label>
                  <Input
                    id="site-name"
                    placeholder={t('sites.namePlaceholder')}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">{t('sites.nameOptional')}</p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="site-max-pages">{t('sites.maxPages')}</Label>
                  <Input
                    id="site-max-pages"
                    type="number"
                    min={1}
                    max={1000}
                    value={maxPages}
                    onChange={(event) => setMaxPages(event.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">{t('sites.maxPagesHint')}</p>
                </div>
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => {
                    setCreateOpen(false)
                    resetForm()
                  }}
                >
                  {t('common.cancel')}
                </Button>
                <Button
                  onClick={() => {
                    void handleCreate()
                    resetForm()
                  }}
                  disabled={!url.trim() || createSite.isPending}
                >
                  {createSite.isPending ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Plus className="mr-2 h-4 w-4" />
                  )}
                  {t('sites.addSite')}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>

          <ConfirmDialog
            open={siteToDelete !== null}
            onOpenChange={(open) => {
              if (!open) setSiteToDelete(null)
            }}
            title={t('sites.deleteSite')}
            description={
              siteToDelete
                ? t('sites.deleteSiteConfirm', { name: siteToDelete.name || siteToDelete.url })
                : ''
            }
            confirmText={t('common.delete')}
            confirmVariant="destructive"
            onConfirm={() => void handleDelete()}
            isLoading={deleteSite.isPending}
          />

          <ConfirmDialog
            open={siteToCrawl !== null}
            onOpenChange={(open) => {
              if (!open) setSiteToCrawl(null)
            }}
            title={t('sites.crawlSite')}
            description={
              siteToCrawl
                ? t('sites.crawlSiteConfirm', { name: siteToCrawl.name || siteToCrawl.url })
                : ''
            }
            confirmText={t('sites.crawl')}
            onConfirm={() => void handleCrawl()}
            isLoading={crawlSite.isPending}
          />
        </div>
      </div>
    </AppShell>
  )
}