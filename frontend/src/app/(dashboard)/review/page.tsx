'use client'

import { AppShell } from '@/components/layout/AppShell'
import { ReviewInbox } from '@/components/review/ReviewInbox'
import { useTranslation } from '@/lib/hooks/use-translation'

export default function ReviewPage() {
  const { t } = useTranslation()

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6 space-y-6">
          <h1 className="font-display text-2xl font-bold tracking-tight">{t('review.title')}</h1>
          <p className="max-w-3xl text-muted-foreground">{t('review.desc')}</p>
          <ReviewInbox />
        </div>
      </div>
    </AppShell>
  )
}
