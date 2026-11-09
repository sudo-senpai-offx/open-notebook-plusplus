'use client'

import { useEffect, useState, useCallback } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { useTranslation } from '@/lib/hooks/use-translation'
import {
  flashcardsApi,
  type Flashcard,
  type FlashcardStatus,
  type FlashcardCreate,
  type FlashcardGenerateRequest,
} from '@/lib/api/flashcards'
import { Plus, Trash2, RefreshCw, Check, X, Mic, FileText, Download } from 'lucide-react'

type View = 'browse' | 'create' | 'generate' | 'export'

export default function FlashcardsPage() {
  const { t } = useTranslation()
  const [view, setView] = useState<View>('browse')
  const [cards, setCards] = useState<Flashcard[]>([])
  const [loading, setLoading] = useState(false)
  [, setFilterStatus] = useState<FlashcardStatus | 'all'>('all')
  const [newCard, setNewCard] = useState<FlashcardCreate>({
    model: 'basic',
    front: '',
    back: '',
    tags: [],
  })
  [generateText, setGenerateText] = useState('')
  [generateCount, setGenerateCount] = useState(10)
  [generateResult, setGenerateResult] = useState<{ ok: boolean; cards: any[]; count: number; error?: string } | null>(null)

  const loadCards = useCallback(async () => {
    setLoading(true)
    try {
      const data = await flashcardsApi.list()
      setCards(data)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadCards()
  }, [loadCards])

  const statusCounts = {
    draft: cards.filter(c => c.status === 'draft').length,
    approved: cards.filter(c => c.status === 'approved').length,
    tombstoned: cards.filter(c => c.status === 'tombstoned').length,
  }

  const filteredCards = cards.filter(c =>
    filterStatus === 'all' || c.status === filterStatus
  )

  const handleCreate = async () => {
    if (newCard.model === 'basic' && (!newCard.front || !newCard.front.trim())) return
    if (newCard.model === 'cloze' && (!newCard.cloze_text || !newCard.cloze_text.trim())) return
    setLoading(true)
    try {
      await flashcardsApi.create(newCard)
      setNewCard({ model: 'basic', front: '', back: '', tags: [] })
      void loadCards()
    } finally {
      setLoading(false)
    }
  }

  const handleApprove = async (cardId: string) => {
    try {
      await flashcardsApi.approve(cardId)
      void loadCards()
    } catch {
      // error handled by toast
    }
  }

  const handleReject = async (cardId: string) => {
    if (!window.confirm(t('flashcards.confirmReject'))) return
    try {
      await flashcardsApi.reject(cardId)
      void loadCards()
    } catch {
      // error handled by toast
    }
  }

  const handleTombstone = async (cardId: string) => {
    if (!window.confirm(t('flashcards.confirmTombstone'))) return
    try {
      await flashcardsApi.tombstone(cardId)
      void loadCards()
    } catch {
      // error handled by toast
    }
  }

  const handleGenerate = async () => {
    if (!generateText.trim()) return
    setLoading(true)
    try {
      const result = await flashcardsApi.generate({
        notebook_id: '', // will be set by backend from context
        source_text: generateText,
        language: 'en',
        card_type: 'basic',
        count: generateCount,
      } as FlashcardGenerateRequest)
      setGenerateResult(result)
    } finally {
      setLoading(false)
    }
  }

  const handleExport = async () => {
    try {
      const blob = await flashcardsApi.export()
      const url = URL.createObjectURL(blob as unknown as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'open-notebook-flashcards.apkg'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch {
      // error handled by toast
    }
  }

  const statusBadge = (status: FlashcardStatus) => {
    const variants: Record<FlashcardStatus, string> = {
      draft: 'bg-yellow-50 text-yellow-700 border-yellow-200',
      approved: 'bg-green-50 text-green-700 border-green-200',
      tombstoned: 'bg-red-50 text-red-700 border-red-200',
    }
    return (
      <Badge variant="secondary" className={variants[status]}>
        {status}
      </Badge>
    )
  }

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6 space-y-6">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="font-display text-2xl font-bold tracking-tight">
                {t('flashcards.title')}
              </h1>
              <p className="text-muted-foreground">{t('flashcards.desc')}</p>
            </div>
            <div className="flex gap-2">
              <Button onClick={() => setView('create')}>
                <Plus className="mr-2 h-4 w-4" />
                {t('flashcards.newCard')}
              </Button>
              <Button onClick={() => setView('generate')}>
                <Mic className="mr-2 h-4 w-4" />
                {t('flashcards.generateAI')}
              </Button>
              <Button onClick={() => setView('export')} variant="outline">
                <Download className="mr-2 h-4 w-4" />
                {t('flashcards.exportApkg')}
              </Button>
            </div>
          </div>

          {/* Status overview */}
          <div className="grid grid-cols-3 gap-4">
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-xs text-muted-foreground">{t('flashcards.drafts')}</p>
                    <p className="text-2xl font-bold">{statusCounts.draft}</p>
                  </div>
                  <Badge variant="outline">draft</Badge>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-xs text-muted-foreground">{t('flashcards.approved')}</p>
                    <p className="text-2xl font-bold">{statusCounts.approved}</p>
                  </div>
                  <Badge variant="secondary">approved</Badge>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-xs text-muted-foreground">{t('flashcards.tombstoned')}</p>
                    <p className="text-2xl font-bold">{statusCounts.tombstoned}</p>
                  </div>
                  <Badge variant="destructive">tombstoned</Badge>
                </div>
              </CardContent>
            </Card>
          </div>

          <Tabs defaultValue="browse" value={view} onValueChange={(v) => setView(v as View)}>
            <TabsList className="grid w-full grid-cols-4">
              <TabsTrigger value="browse">{t('flashcards.browse')}</TabsTrigger>
              <TabsTrigger value="create">{t('flashcards.create')}</TabsTrigger>
              <TabsTrigger value="generate">{t('flashcards.generate')}</TabsTrigger>
              <TabsTrigger value="export">{t('flashcards.export')}</TabsTrigger>
            </TabsList>

            {/* Browse */}
            <TabsContent value="browse">
              <div className="space-y-4">
                <div className="flex items-center gap-4">
                  <Select
                    defaultValue="all"
                    onValueChange={(v) => setFilterStatus(v as FlashcardStatus | 'all')}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder={t('flashcards.filterStatus')} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">{t('flashcards.allStatus')}</SelectItem>
                      <SelectItem value="draft">{t('flashcards.statusDraft')}</SelectItem>
                      <SelectItem value="approved">{t('flashcards.statusApproved')}</SelectItem>
                      <SelectItem value="tombstoned">{t('flashcards.statusTombstoned')}</SelectItem>
                    </SelectContent>
                  </Select>
                  <Button onClick={() => void loadCards()} size="sm">
                    <RefreshCw className="mr-2 h-4 w-4" />
                    {t('flashcards.refresh')}
                  </Button>
                </div>

                {loading ? (
                  <p className="text-sm text-muted-foreground">{t('flashcards.loading')}</p>
                ) : filteredCards.length === 0 ? (
                  <div className="text-center py-12 text-muted-foreground">
                    <p>{t('flashcards.noCards')}</p>
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm text-left">
                      <thead>
                        <tr className="border-b border-border">
                          <th className="pb-3 text-muted-foreground">{t('flashcards.front')}</th>
                          <th className="pb-3 text-muted-foreground">{t('flashcards.back')}</th>
                          <th className="pb-3 text-muted-foreground">{t('flashcards.model')}</th>
                          <th className="pb-3 text-muted-foreground">{t('flashcards.tags')}</th>
                          <th className="pb-3 text-muted-foreground">{t('flashcards.status')}</th>
                          <th className="pb-3 text-muted-foreground text-right">{t('flashcards.actions')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredCards.map((card) => (
                          <tr key={card.id} className="border-b border-border hover:bg-accent/50">
                            <td className="py-3">
                              {card.model === 'cloze' ? card.cloze_text : card.front} || '-'
                            </td>
                            <td className="py-3">
                              {card.model === 'cloze' ? t('flashcards.clozeCard') : card.back || '-'}
                            </td>
                            <td className="py-3">{card.model}</td>
                            <td className="py-3">
                              {card.tags.length > 0 ? card.tags.join(', ') : '-'}
                            </td>
                            <td className="py-3">{statusBadge(card.status)}</td>
                            <td className="py-3 text-right flex gap-2 justify-end">
                              {card.status === 'draft' && (
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() => handleApprove(card.id)}
                                >
                                  <Check className="mr-1 h-4 w-4" />
                                  {t('flashcards.approve')}
                                </Button>
                              )}
                              {card.status === 'draft' && (
                                <Button
                                  size="sm"
                                  variant="destructive"
                                  onClick={() => handleReject(card.id)}
                                >
                                  <X className="mr-1 h-4 w-4" />
                                  {t('flashcards.reject')}
                                </Button>
                              )}
                              {card.status === 'approved' && (
                                <Button
                                  size="sm"
                                  variant="destructive"
                                  onClick={() => handleTombstone(card.id)}
                                >
                                  <Trash2 className="mr-1 h-4 w-4" />
                                  {t('flashcards.tombstone')}
                                </Button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </TabsContent>

            {/* Create */}
            <TabsContent value="create">
              <Card>
                <CardHeader>
                  <CardTitle>{t('flashcards.newCard')}</CardTitle>
                  <CardDescription>{t('flashcards.newCardDesc')}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <Select
                    defaultValue="basic"
                    onValueChange={(v) =>
                      setNewCard({ ...newCard, model: v as 'basic' | 'cloze' })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue placeholder={t('flashcards.modelType')} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="basic">{t('flashcards.basic')}</SelectItem>
                      <SelectItem value="cloze">{t('flashcards.cloze')}</SelectItem>
                    </SelectContent>
                  </Select>

                  {newCard.model === 'basic' ? (
                    <>
                      <Input
                        label={t('flashcards.front')}
                        placeholder={t('flashcards.frontPlaceholder')}
                        value={newCard.front ?? ''}
                        onChange={(e) => setNewCard({ ...newCard, front: e.target.value })}
                      />
                      <Input
                        label={t('flashcards.back')}
                        placeholder={t('flashcards.backPlaceholder')}
                        value={newCard.back ?? ''}
                        onChange={(e) => setNewCard({ ...newCard, back: e.target.value })}
                      />
                    </>
                  ) : (
                    <Textarea
                      label={t('flashcards.clozeText')}
                      placeholder={t('flashcards.clozePlaceholder')}
                      value={newCard.cloze_text ?? ''}
                      onChange={(e) =>
                        setNewCard({ ...newCard, cloze_text: e.target.value })
                      />
                    />
                  )}

                  <Input
                    label={t('flashcards.tags')}
                    placeholder={t('flashcards.tagsPlaceholder')}
                    value={newCard.tags.join(', ')}
                    onChange={(e) =>
                      setNewCard({
                        ...newCard,
                        tags: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                      })
                    />
                  />

                  <Button
                    onClick={handleCreate}
                    disabled={loading}
                    className="w-full"
                  >
                    {loading ? t('flashcards.creating') : t('flashcards.createCard')}
                  </Button>
                </CardContent>
              </Card>
            </TabsContent>

            {/* Generate */}
            <TabsContent value="generate">
              <Card>
                <CardHeader>
                  <CardTitle>{t('flashcards.generateAI')}</CardTitle>
                  <CardDescription>{t('flashcards.generateAIDesc')}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <Textarea
                    label={t('flashcards.sourceText')}
                    placeholder={t('flashcards.sourceTextPlaceholder')}
                    value={generateText}
                    onChange={(e) => setGenerateText(e.target.value)}
                    className="h-48"
                  />
                  <div className="grid grid-cols-2 gap-4">
                    <Select defaultValue="en" onValueChange={() => {}}>
                      <SelectTrigger>
                        <SelectValue placeholder="Language" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="en">English</SelectItem>
                        <SelectItem value="ja">Japanese</SelectItem>
                      </SelectContent>
                    </Select>
                    <Input
                      type="number"
                      label={t('flashcards.count')}
                      value={generateCount}
                      onChange={(e) => setGenerateCount(parseInt(e.target.value) || 10)}
                    />
                  </div>
                  <Button
                    onClick={handleGenerate}
                    disabled={loading || !generateText.trim()}
                    className="w-full"
                  >
                    {loading ? t('flashcards.generating') : t('flashcards.generateCards')}
                  </Button>
                </CardContent>
              </Card>

              {generateResult && (
                <Card>
                  <CardHeader>
                    <CardTitle>
                      {t('flashcards.generatedCards', { count: generateResult.count })}
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    {generateResult.error && (
                      <div className="p-3 bg-red-50 text-red-700 rounded">
                        {generateResult.error}
                      </div>
                    )}
                    {generateResult.ok && generateResult.cards.length > 0 && (
                      <div className="space-y-3">
                        {generateResult.cards.map((card, i) => (
                          <div key={i} className="p-3 border rounded bg-card">
                            <p className="text-sm font-medium">{card.front}</p>
                            {card.cloze_text ? (
                              <p className="text-sm text-muted-foreground mt-1">
                                {card.cloze_text}
                              </p>
                            ) : (
                              <p className="text-sm text-muted-foreground mt-1">
                                {card.back}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </CardContent>
                </Card>
              )}
            </TabsContent>

            {/* Export */}
            <TabsContent value="export">
              <Card>
                <CardHeader>
                  <CardTitle>{t('flashcards.exportApkg')}</CardTitle>
                  <CardDescription>{t('flashcards.exportApkgDesc')}</CardDescription>
                </CardHeader>
                <CardContent>
                  <Input
                    label={t('flashcards.deckName')}
                    placeholder="OpenNotebook"
                    defaultValue="OpenNotebook"
                  />
                  <Button onClick={handleExport} className="w-full mt-4">
                    <Download className="mr-2 h-4 w-4" />
                    {t('flashcards.downloadApkg')}
                  </Button>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>
      </div>
    </AppShell>
  )
}
