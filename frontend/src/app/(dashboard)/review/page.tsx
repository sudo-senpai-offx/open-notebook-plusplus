'use client'

import { useState } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { ReviewInbox } from '@/components/review/ReviewInbox'
import { ReviewQueue } from '@/components/review/ReviewQueue'
import { AuthorCard } from '@/components/review/AuthorCard'
import { useTranslation } from '@/lib/hooks/use-translation'

type Tab = 'inbox' | 'queue' | 'author'

export default function ReviewPage() {
  const { t } = useTranslation()
  const [tab, setTab] = useState<Tab>('queue')
  const [authorKey, setAuthorKey] = useState(0)

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
            <Button
              role="tab"
              aria-selected={tab === 'author'}
              variant={tab === 'author' ? 'default' : 'outline'}
              size="sm"
              onClick={() => setTab('author')}
            >
              {t('review.author')}
            </Button>
          </div>
          {tab === 'queue' ? (
            <ReviewQueue />
          ) : tab === 'inbox' ? (
            <ReviewInbox />
          ) : (
            <AuthorCard key={authorKey} onCreated={() => setAuthorKey(k => k + 1)} />
          )}
        </div>
      </div>
    </AppShell>
  )
}
