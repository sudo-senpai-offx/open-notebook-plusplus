'use client'

import { useEffect, useState } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { useTranslation } from '@/lib/hooks/use-translation'
import { syncApi } from '@/lib/api/settings'
import { Alert, AlertTitle, AlertDescription } from '@/components/ui/alert'

interface SyncConflict {
  collection: string
  db_record_id: string
  local_updated?: string
  remote_updated?: string
  resolved: boolean
  resolution?: string
}

export default function SyncPage() {
  const { t } = useTranslation()
  const [conflicts, setConflicts] = useState<SyncConflict[]>([])
  const [loading, setLoading] = useState(true)
  const [optout, setOptout] = useState<{ collections: string[]; record_ids: string[] }>({ collections: [], record_ids: [] })

  useEffect(() => {
    let mounted = true
    const load = async () => {
      try {
        const [c, o] = await Promise.all([syncApi.getConflicts(), syncApi.getOptout()])
        if (mounted) {
          setConflicts(c)
          setOptout(o)
        }
      } catch {
        if (mounted) setConflicts([])
      } finally {
        if (mounted) setLoading(false)
      }
    }
    load()
    return () => { mounted = false }
  }, [])

  const resolveConflict = async (resolution: 'local' | 'remote', recordId: string) => {
    try {
      await syncApi.resolveConflict(resolution)
      setConflicts(prev => prev.filter(c => c.db_record_id !== recordId))
    } catch {
      // error handled by toast in a real app
    }
  }

  const toggleOptoutCollection = async (collection: string) => {
    const newCollections = optout.collections.includes(collection)
      ? optout.collections.filter(c => c !== collection)
      : [...optout.collections, collection]
    try {
      const updated = await syncApi.updateOptout(newCollections, optout.record_ids)
      setOptout(updated)
    } catch {
      // error
    }
  }

  if (loading) {
    return (
      <AppShell>
        <div className="flex items-center justify-center min-h-[60vh]">
          <div className="flex flex-col items-center gap-2">
            <div className="h-4 w-4 animate-spin rounded-full border-4 border-primary border-t-transparent" />
            <span>{t('settings.syncLoading')}</span>
          </div>
        </div>
      </AppShell>
    )
  }

  const pendingConflicts = conflicts.filter(c => !c.resolved)

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6">
          <div className="max-w-4xl">
            <div className="flex items-center gap-4 mb-6">
              <h1 className="font-display text-2xl font-bold tracking-tight">{t('settings.sync')}</h1>
            </div>

            {/* Conflict Review */}
            <Card>
              <CardHeader>
                <CardTitle>{t('settings.syncConflicts')}</CardTitle>
                <CardDescription>{t('settings.syncConflictsDesc')}</CardDescription>
              </CardHeader>
              <CardContent>
                {pendingConflicts.length === 0 ? (
                  <div className="text-center py-8 text-muted-foreground">
                    <p>{t('settings.noSyncConflicts')}</p>
                  </div>
                ) : (
                  <div className="space-y-4">
                    {pendingConflicts.map(conflict => (
                      <div key={conflict.db_record_id} className="border rounded-lg p-4">
                        <div className="flex justify-between items-start mb-2">
                          <span className="font-mono text-xs text-muted-foreground truncate max-w-2xs">
                            {conflict.db_record_id}
                          </span>
                          <Badge variant="destructive">{t('settings.unresolved')}</Badge>
                        </div>
                        <div className="grid grid-cols-2 gap-4 mb-3 text-xs">
                          <div>
                            <p className="text-muted-foreground">{t('settings.local')}</p>
                            <p className="font-mono">{conflict.local_updated ?? '-'}</p>
                          </div>
                          <div>
                            <p className="text-muted-foreground">{t('settings.remote')}</p>
                            <p className="font-mono">{conflict.remote_updated ?? '-'}</p>
                          </div>
                        </div>
                        <div className="flex gap-2">
                          <Button
                            onClick={() => resolveConflict('local', conflict.db_record_id)}
                            variant="outline"
                            size="sm"
                          >
                            {t('settings.keepLocal')}
                          </Button>
                          <Button
                            onClick={() => resolveConflict('remote', conflict.db_record_id)}
                            variant="outline"
                            size="sm"
                          >
                            {t('settings.takeRemote')}
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Opt-Out Configuration */}
            <Card>
              <CardHeader>
                <CardTitle>{t('settings.syncOptOut')}</CardTitle>
                <CardDescription>{t('settings.syncOptOutDesc')}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <p className="text-sm text-muted-foreground">{t('settings.syncOptOutCollections')}</p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {['notes', 'cards', 'sources', 'notebooks', 'insights', 'chats'].map(collection => (
                    <Button
                      key={collection}
                      onClick={() => toggleOptoutCollection(collection)}
                      variant={optout.collections.includes(collection) ? 'destructive' : 'outline'}
                      size="sm"
                    >
                      {optout.collections.includes(collection) ? '✓ ' : ''}{collection}
                    </Button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {t('settings.syncOptOutNote')}
                </p>
              </CardContent>
            </Card>

            {/* Auto Sync Button */}
            <Card>
              <CardHeader>
                <CardTitle>{t('settings.syncAuto')}</CardTitle>
                <CardDescription>{t('settings.syncAutoDesc')}</CardDescription>
              </CardHeader>
              <CardContent>
                <Button onClick={async () => {
                  try {
                    await syncApi.autoSync()
                  } catch {
                    // error
                  }
                }}>
                  {t('settings.runAutoSync')}
                </Button>
                <p className="text-xs text-muted-foreground mt-2">
                  {t('settings.syncAutoNote')}
                </p>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </AppShell>
  )
}
