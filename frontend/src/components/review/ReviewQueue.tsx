'use client'

import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useTranslation } from '@/lib/hooks/use-translation'
import { reviewApi, type DueCard, type ReviewRating } from '@/lib/api/review'

const GRADES: ReviewRating[] = ['again', 'hard', 'good', 'easy']

// Static key map (never constructed dynamically): the unused-key test and
// extraction tooling only see literal t() keys.
const GRADE_KEYS: Record<ReviewRating, string> = {
  again: 'review.grade_again',
  hard: 'review.grade_hard',
  good: 'review.grade_good',
  easy: 'review.grade_easy',
}

function prompt(card: DueCard): string {
  if (card.model === 'cloze') return card.cloze_text ?? ''
  return card.front ?? ''
}

function answer(card: DueCard): string {
  if (card.model === 'cloze') return card.cloze_text ?? ''
  return card.back ?? ''
}

export function ReviewQueue() {
  const { t } = useTranslation()
  const [cards, setCards] = useState<DueCard[]>([])
  const [index, setIndex] = useState(0)
  const [revealed, setRevealed] = useState(false)
  const [graded, setGraded] = useState(0)
  const [loading, setLoading] = useState(true)
  const [shownAt, setShownAt] = useState<number>(0)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const due = await reviewApi.due()
      setCards(due)
      setIndex(0)
      setRevealed(false)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (cards[index]) setShownAt(Date.now())
  }, [cards, index])

  const grade = async (rating: ReviewRating) => {
    const card = cards[index]
    if (!card) return
    const durationMs = Date.now() - shownAt
    await reviewApi.grade(card.id, rating, durationMs)
    setGraded(g => g + 1)
    setRevealed(false)
    setIndex(i => i + 1)
  }

  if (loading) {
    return <p className="text-sm text-muted-foreground">{t('review.loading')}</p>
  }

  const card = cards[index]

  return (
    <div className="max-w-3xl space-y-4">
      <p className="text-sm text-muted-foreground">
        {t('review.sessionProgress', { graded, total: cards.length })}
      </p>
      {!card && (
        <p className="text-sm text-muted-foreground">{t('review.queueEmpty')}</p>
      )}
      {card && (
        <Card>
          <CardContent className="space-y-4 pt-4">
            <p className="text-lg">{prompt(card)}</p>
            {revealed ? (
              <>
                <p className="border-t pt-4 text-lg font-medium">{answer(card)}</p>
                <div className="flex flex-wrap gap-2">
                  {GRADES.map(g => (
                    <Button key={g} size="sm" variant={g === 'good' ? 'default' : 'outline'} onClick={() => void grade(g)}>
                      {t(GRADE_KEYS[g])}
                    </Button>
                  ))}
                </div>
              </>
            ) : (
              <Button onClick={() => setRevealed(true)}>{t('review.reveal')}</Button>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
