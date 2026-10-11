'use client'

import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import { useTranslation } from '@/lib/hooks/use-translation'
import { flashcardsApi, type Flashcard, type FlashcardStatus } from '@/lib/api/flashcards'

const TABS: FlashcardStatus[] = ['draft', 'approved', 'tombstoned']

// Static key map (never constructed dynamically): the unused-key test and
// extraction tooling only see literal t() keys.
const TAB_KEYS: Record<FlashcardStatus, string> = {
  draft: 'review.tab_draft',
  approved: 'review.tab_approved',
  tombstoned: 'review.tab_tombstoned',
}

function cardText(card: Flashcard): string {
  if (card.model === 'cloze') return card.cloze_text ?? ''
  return [card.front ?? '', card.back ?? ''].filter(Boolean).join(' — ')
}

export function ReviewInbox() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<FlashcardStatus>('draft')
  const [cards, setCards] = useState<Flashcard[]>([])
  const [loading, setLoading] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editText, setEditText] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setCards(await flashcardsApi.list({ status }))
    } finally {
      setLoading(false)
    }
  }, [status])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const act = async (fn: () => Promise<unknown>) => {
    await fn()
    await refresh()
  }

  return (
    <div className="max-w-3xl space-y-4">
      <div className="flex gap-2" role="tablist" aria-label={t('review.inbox')}>
        {TABS.map(tab => (
          <Button
            key={tab}
            role="tab"
            aria-selected={status === tab}
            variant={status === tab ? 'default' : 'outline'}
            size="sm"
            onClick={() => setStatus(tab)}
          >
            {t(TAB_KEYS[tab])}
          </Button>
        ))}
      </div>
      {loading && <p className="text-sm text-muted-foreground">{t('review.loading')}</p>}
      {!loading && cards.length === 0 && (
        <p className="text-sm text-muted-foreground">{t('review.empty')}</p>
      )}
      {cards.map(card => (
        <Card key={card.id}>
          <CardContent className="space-y-2 pt-4">
            <p className="text-sm">{cardText(card)}</p>
            <p className="text-xs text-muted-foreground">
              {card.model} · {card.guid ?? t('review.noGuid')}
            </p>
            {editingId === card.id ? (
              <div className="space-y-2">
                <Textarea
                  aria-label={t('review.editText')}
                  value={editText}
                  onChange={e => setEditText(e.target.value)}
                  rows={3}
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() =>
                      act(async () => {
                        const patch =
                          card.model === 'cloze'
                            ? { cloze_text: editText }
                            : { front: editText };
                        await flashcardsApi.update(card.id, patch)
                        setEditingId(null)
                      })
                    }
                  >
                    {t('review.save')}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setEditingId(null)}>
                    {t('review.cancel')}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {card.status === 'draft' && (
                  <>
                    <Button size="sm" onClick={() => act(() => flashcardsApi.approve(card.id))}>
                      {t('review.approve')}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setEditingId(card.id)
                        setEditText(
                          card.model === 'cloze' ? (card.cloze_text ?? '') : (card.front ?? '')
                        )
                      }}
                    >
                      {t('review.edit')}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => act(() => flashcardsApi.reject(card.id))}>
                      {t('review.reject')}
                    </Button>
                  </>
                )}
                {card.status === 'approved' && (
                  <Button size="sm" variant="outline" onClick={() => act(() => flashcardsApi.tombstone(card.id))}>
                    {t('review.tombstone')}
                  </Button>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
