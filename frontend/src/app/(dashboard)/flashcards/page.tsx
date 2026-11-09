'use client'

import { useCallback, useEffect, useState } from 'react'
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
import {
  flashcardsApi,
  type Flashcard,
  type FlashcardStatus,
  type FlashcardCreate,
  type FlashcardGenerateRequest,
} from '@/lib/api/flashcards'
import { Plus, Trash2, RefreshCw, Check, X, Mic, Download } from 'lucide-react'

type View = 'browse' | 'create' | 'generate' | 'export'

export default function FlashcardsPage() {
  const [view, setView] = useState<View>('browse')
  const [cards, setCards] = useState<Flashcard[]>([])
  const [loading, setLoading] = useState(false)
  const [_filterStatus, setFilterStatus] = useState<FlashcardStatus | 'all'>('all')
  const [newCard, setNewCard] = useState<FlashcardCreate>({
    model: 'basic',
    front: '',
    back: '',
    tags: [],
  })
  const [generateText, setGenerateText] = useState('')
  const [generateCount, setGenerateCount] = useState(10)
  const [generateResult, setGenerateResult] = useState<{ ok: boolean; cards: any[]; count: number; error?: string } | null>(null)

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
    _filterStatus === 'all' || c.status === _filterStatus
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
    if (!window.confirm('Rejecting deletes this draft permanently. Continue?')) return
    try {
      await flashcardsApi.reject(cardId)
      void loadCards()
    } catch {
      // error handled by toast
    }
  }

  const handleTombstone = async (cardId: string) => {
    if (!window.confirm('Tombstoning excludes this card from reviews and exports. Continue?')) return
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
        notebook_id: '',
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
                Flashcards
              </h1>
              <p className="text-muted-foreground">
                Create, review, and manage your flashcard deck. Cards start as drafts and must be
                approved before they appear in reviews.
              </p>
            </div>
            <div className="flex gap-2">
              <Button onClick={() => setView('create')}>
                <Plus className="mr-2 h-4 w-4" />
                New Card
              </Button>
              <Button onClick={() => setView('generate')}>
                <Mic className="mr-2 h-4 w-4" />
                Generate with AI
              </Button>
              <Button onClick={() => setView('export')} variant="outline">
                <Download className="mr-2 h-4 w-4" />
                Export .apkg
              </Button>
            </div>
          </div>

          {/* Status overview */}
          <div className="grid grid-cols-3 gap-4">
            <Card>
              <CardContent className="p-4">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="text-xs text-muted-foreground">Drafts</p>
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
                    <p className="text-xs text-muted-foreground">Approved</p>
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
                    <p className="text-xs text-muted-foreground">Tombstoned</p>
                    <p className="text-2xl font-bold">{statusCounts.tombstoned}</p>
                  </div>
                  <Badge variant="destructive">tombstoned</Badge>
                </div>
              </CardContent>
            </Card>
          </div>

          <Tabs defaultValue="browse" value={view} onValueChange={(v) => setView(v as View)}>
            <TabsList className="grid w-full grid-cols-4">
              <TabsTrigger value="browse">Browse</TabsTrigger>
              <TabsTrigger value="create">Create</TabsTrigger>
              <TabsTrigger value="generate">Generate</TabsTrigger>
              <TabsTrigger value="export">Export</TabsTrigger>
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
                      <SelectValue placeholder="Filter by status" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">All</SelectItem>
                      <SelectItem value="draft">Draft</SelectItem>
                      <SelectItem value="approved">Approved</SelectItem>
                      <SelectItem value="tombstoned">Tombstoned</SelectItem>
                    </SelectContent>
                  </Select>
                  <Button onClick={() => void loadCards()} size="sm">
                    <RefreshCw className="mr-2 h-4 w-4" />
                    Refresh
                  </Button>
                </div>

                {loading ? (
                  <p className="text-sm text-muted-foreground">Loading cards…</p>
                ) : filteredCards.length === 0 ? (
                  <div className="text-center py-12 text-muted-foreground">
                    <p>No flashcards yet. Create your first card or generate from source text.</p>
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm text-left">
                      <thead>
                        <tr className="border-b border-border">
                          <th className="pb-3 text-muted-foreground">Front</th>
                          <th className="pb-3 text-muted-foreground">Back</th>
                          <th className="pb-3 text-muted-foreground">Model</th>
                          <th className="pb-3 text-muted-foreground">Tags</th>
                          <th className="pb-3 text-muted-foreground">Status</th>
                          <th className="pb-3 text-muted-foreground text-right">
                            Actions
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredCards.map((card) => (
                          <tr key={card.id} className="border-b border-border hover:bg-accent/50">
                            <td className="py-3">
                              {card.model === 'cloze' ? card.cloze_text : card.front} || '-'
                            </td>
                            <td className="py-3">
                              {card.model === 'cloze' ? 'Cloze card' : card.back || '-'}
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
                                  Approve
                                </Button>
                              )}
                              {card.status === 'draft' && (
                                <Button
                                  size="sm"
                                  variant="destructive"
                                  onClick={() => handleReject(card.id)}
                                >
                                  <X className="mr-1 h-4 w-4" />
                                  Reject
                                </Button>
                              )}
                              {card.status === 'approved' && (
                                <Button
                                  size="sm"
                                  variant="destructive"
                                  onClick={() => handleTombstone(card.id)}
                                >
                                  <Trash2 className="mr-1 h-4 w-4" />
                                  Tombstone
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
                  <CardTitle>New Card</CardTitle>
                  <CardDescription>
                    Author a flashcard manually. Cards start as drafts.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <Select
                    defaultValue="basic"
                    onValueChange={(v) =>
                      setNewCard({ ...newCard, model: v as 'basic' | 'cloze' })
                    }
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Card type" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="basic">Basic</SelectItem>
                      <SelectItem value="cloze">Cloze</SelectItem>
                    </SelectContent>
                  </Select>

                  {newCard.model === 'basic' ? (
                    <>
                      <div className="space-y-2">
                        <label className="text-xs font-medium text-muted-foreground">
                          Front
                        </label>
                        <Input
                          placeholder="Question or prompt"
                          value={newCard.front ?? ''}
                          onChange={(e) =>
                            setNewCard({ ...newCard, front: e.target.value })}
                        />
                      </div>
                      <div className="space-y-2">
                        <label className="text-xs font-medium text-muted-foreground">
                          Back
                        </label>
                        <Input
                          placeholder="Answer"
                          value={newCard.back ?? ''}
                          onChange={(e) =>
                            setNewCard({ ...newCard, back: e.target.value })}
                        />
                      </div>
                    </>
                  ) : (
                    <div className="space-y-2">
                      <label className="text-xs font-medium text-muted-foreground">
                        Cloze text
                      </label>
                      <Textarea
                        placeholder="The {{c1::answer}} is the capital of France."
                        value={newCard.cloze_text ?? ''}
                        onChange={(e) =>
                          setNewCard({ ...newCard, cloze_text: e.target.value })}
                        className="h-32"
                      />
                    </div>
                  )}

                  <div className="space-y-2">
                    <label className="text-xs font-medium text-muted-foreground">
                      Tags
                    </label>
                    <Input
                      placeholder="tag1, tag2"
                      value={newCard.tags?.join(', ') ?? ''}
                      onChange={(e) => {
                        setNewCard({
                          ...newCard,
                          tags: e.target.value
                            .split(',')
                            .map((s) => s.trim())
                            .filter(Boolean),
                        })
                      }}
                    />
                  </div>

                  <Button
                    onClick={handleCreate}
                    disabled={loading}
                    className="w-full"
                  >
                    {loading ? 'Creating…' : 'Create Card'}
                  </Button>
                </CardContent>
              </Card>
            </TabsContent>

            {/* Generate */}
            <TabsContent value="generate">
              <Card>
                <CardHeader>
                  <CardTitle>Generate with AI</CardTitle>
                  <CardDescription>
                    Generate flashcards from source text via DeepSeek-web.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="space-y-2">
                    <label className="text-xs font-medium text-muted-foreground">
                      Source text
                    </label>
                    <Textarea
                      placeholder="Paste source text to generate flashcards from"
                      value={generateText}
                      onChange={(e) => setGenerateText(e.target.value)}
                      className="h-48"
                    />
                  </div>
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
                    <div className="space-y-2">
                      <label className="text-xs font-medium text-muted-foreground">
                        Count
                      </label>
                      <Input
                        type="number"
                        value={generateCount}
                        onChange={(e) =>
                          setGenerateCount(parseInt(e.target.value) || 10)}
                      />
                    </div>
                  </div>
                  <Button
                    onClick={handleGenerate}
                    disabled={loading || !generateText.trim()}
                    className="w-full"
                  >
                    {loading ? 'Generating…' : 'Generate Cards'}
                  </Button>
                </CardContent>
              </Card>

              {generateResult && (
                <Card>
                  <CardHeader>
                    <CardTitle>
                      Generated {generateResult.count} cards
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
                  <CardTitle>Export .apkg</CardTitle>
                  <CardDescription>
                    Export approved cards as an Anki .apkg package.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    <label className="text-xs font-medium text-muted-foreground">
                      Deck name
                    </label>
                    <Input
                      placeholder="OpenNotebook"
                      defaultValue="OpenNotebook"
                    />
                  </div>
                  <Button onClick={handleExport} className="w-full mt-4">
                    <Download className="mr-2 h-4 w-4" />
                    Download .apkg
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
