'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useTranslation } from '@/lib/hooks/use-translation'
import { flashcardsApi } from '@/lib/api/flashcards'

// Static key map (never constructed dynamically): the unused-key test and
// extraction tooling only see literal t() keys.
const MODEL_KEYS = {
  basic: 'review.model_basic',
  cloze: 'review.model_cloze',
} as const

export function AuthorCard({ onCreated }: { onCreated?: () => void }) {
  const { t } = useTranslation()
  const [model, setModel] = useState<'basic' | 'cloze'>('basic')
  const [front, setFront] = useState('')
  const [back, setBack] = useState('')
  const [clozeText, setClozeText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    setError(null)
    setSaving(true)
    try {
      if (model === 'basic') {
        await flashcardsApi.create({ model, front, back })
      } else {
        await flashcardsApi.create({ model, cloze_text: clozeText })
      }
      setFront('')
      setBack('')
      setClozeText('')
      onCreated?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('review.authorFailed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card className="max-w-3xl">
      <CardContent className="space-y-4 pt-4">
        <div className="flex gap-2" role="tablist" aria-label={t('review.model')}>
          {(['basic', 'cloze'] as const).map(m => (
            <Button
              key={m}
              size="sm"
              variant={model === m ? 'default' : 'outline'}
              onClick={() => setModel(m)}
            >
              {t(MODEL_KEYS[m])}
            </Button>
          ))}
        </div>
        {model === 'basic' ? (
          <>
            <Input
              aria-label={t('review.front')}
              value={front}
              onChange={e => setFront(e.target.value)}
              placeholder={t('review.frontPlaceholder')}
            />
            <Input
              aria-label={t('review.back')}
              value={back}
              onChange={e => setBack(e.target.value)}
              placeholder={t('review.backPlaceholder')}
            />
          </>
        ) : (
          <Textarea
            aria-label={t('review.clozeText')}
            value={clozeText}
            onChange={e => setClozeText(e.target.value)}
            placeholder={t('review.clozePlaceholder')}
            rows={4}
          />
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
        <Button onClick={() => void submit()} disabled={saving}>
          {t('review.authorSave')}
        </Button>
      </CardContent>
    </Card>
  )
}
