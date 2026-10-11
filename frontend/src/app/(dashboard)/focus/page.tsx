'use client'

import { AppShell } from '@/components/layout/AppShell'
import { FocusTimer } from '@/components/focus/FocusTimer'
import { useTranslation } from '@/lib/hooks/use-translation'

export default function FocusPage() {
  const { t } = useTranslation()

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6 space-y-6">
          <h1 className="font-display text-2xl font-bold tracking-tight">{t('focus.title')}</h1>
          <FocusTimer />
        </div>
      </div>
    </AppShell>
  )
}
