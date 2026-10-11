'use client'

import { useState } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { ReviewInbox } from '@/components/review/ReviewInbox'
import { ReviewQueue } from '@/components/review/ReviewQueue'
import { useTranslation } from '@/lib/hooks/use-translation'

type Tab = 'inbox' | 'queue'

export default function ReviewPage() {
  const { t } = useTranslation()
  const [tab, setTab] = useState<Tab>('queue')

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6 space-y-6">
          <h1 className="font-display text-2xl font-bold tracking-tight">{t('review.title')}</h1>
          <p className="max-w-3xl text-muted-foreground">{t('review.desc')}</p>
          <div className="flex gap-2" role="tablist" aria-label={t('review.title')}>
            <Button
              role="tab"
              aria-selected={tab === 'queue'}
              variant={tab === 'queue' ? 'default' : 'outline'}
              size="sm"
              onClick={() => setTab('queue')}
            >
              {t('review.queue')}
            </Button>
            <Button
              role="tab"
              aria-selected={tab === 'inbox'}
              variant={tab === 'inbox' ? 'default' : 'outline'}
              size="sm"
              onClick={() => setTab('inbox')}
            >
              {t('review.inbox')}
            </Button>
          </div>
          {tab === 'queue' ? <ReviewQueue /> : <ReviewInbox />}
        </div>
      </div>
    </AppShell>
  )
}
