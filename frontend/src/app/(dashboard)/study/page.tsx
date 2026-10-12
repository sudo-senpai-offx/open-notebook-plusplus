'use client'

import { AppShell } from '@/components/layout/AppShell'
import { StudyDashboard } from '@/components/study/StudyDashboard'
import { useTranslation } from '@/lib/hooks/use-translation'

export default function StudyPage() {
  const { t } = useTranslation()

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6 space-y-6">
          <h1 className="font-display text-2xl font-bold tracking-tight">{t('study.title')}</h1>
          <p className="max-w-3xl text-muted-foreground">{t('study.desc')}</p>
          <StudyDashboard />
        </div>
      </div>
    </AppShell>
  )
}
