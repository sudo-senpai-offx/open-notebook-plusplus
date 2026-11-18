import { createSignal, onMount, Show, For } from 'solid-js'
import { t } from '@/lib/i18n'
import { sourceApi, sourceChatApi, siteApi } from '@/lib/api/settings'
import { streamSse } from '@/lib/api/sse'
import { ChatPanel } from '@/components/sources/ChatPanel'
import type { Source, SourceChatSession, ChatMessage, Site, CreateSiteRequest } from '@/lib/types/api'

export function SourcesPage() {
  const SESSION_STORAGE_KEY = 'onb_last_source_id'
  const SESSION_STORAGE_SESSION_KEY = 'onb_last_session_id'

  // --- Sources state ---
  const [sources, setSources] = createSignal<Source[]>([])
  const [selectedSource, setSelectedSource] = createSignal<Source | null>(null)
  const [sessions, setSessions] = createSignal<SourceChatSession[]>([])
  const [selectedSession, setSelectedSession] =
    createSignal<SourceChatSession | null>(null)
  const [messages, setMessages] = createSignal<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = createSignal(false)
  const [loading, setLoading] = createSignal(true)
  const [error, setError] = createSignal('')

  // --- Sites state ---
  const [sites, setSites] = createSignal<Site[]>([])
  const [selectedSite, setSelectedSite] = createSignal<Site | null>(null)
  const [sitesLoading, setSitesLoading] = createSignal(false)
  const [newSiteUrl, setNewSiteUrl] = createSignal('')
  const [newSiteName, setNewSiteName] = createSignal('')
  const [siteError, setSiteError] = createSignal('')

  // --- Tab state ---
  const [activeTab, setActiveTab] = createSignal<'sources' | 'sites'>('sources')

  const loadSources = async () => {
    try {
      const list = await sourceApi.list()
      setSources(list)
      if (list.length > 0) {
        await loadSessions(list[0])
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  const loadSites = async () => {
    setSitesLoading(true)
    try {
      const list = await siteApi.list()
      setSites(list)
    } catch (e) {
      setSiteError(String(e))
    } finally {
      setSitesLoading(false)
    }
  }

  onMount(async () => {
    try {
      await Promise.all([loadSources(), loadSites()])
      const lastSourceId = localStorage.getItem(SESSION_STORAGE_KEY)
      const lastSessionId = localStorage.getItem(SESSION_STORAGE_SESSION_KEY)
      if (lastSourceId && lastSessionId) {
        const src = sources().find(s => s.id === lastSourceId)
        if (src) {
          await loadSessions(src)
          const sess = sessions().find(s => s.id === lastSessionId)
          if (sess) {
            setSelectedSession(sess)
            await loadMessages(src.id, sess.id)
          }
        }
      }
    } catch (e) {
      setError(String(e))
    }
  })

  const loadSessions = async (src: Source) => {
    setSelectedSource(src)
    localStorage.setItem(SESSION_STORAGE_KEY, src.id)
    try {
      const list = await sourceChatApi.listSessions(src.id)
      setSessions(list)
      if (list.length > 0) {
        await loadMessages(src.id, list[0].id)
        setSelectedSession(list[0])
        localStorage.setItem(SESSION_STORAGE_SESSION_KEY, list[0].id)
      } else {
        setSelectedSession(null)
        setMessages([])
        localStorage.removeItem(SESSION_STORAGE_SESSION_KEY)
      }
    } catch {
      setSessions([])
    }
  }

  const loadMessages = async (sourceId: string, sessionId: string) => {
    try {
      const full = await sourceChatApi.getSession(sourceId, sessionId)
      setMessages(full.messages || [])
    } catch {
      setMessages([])
    }
  }

  const handleSelectSession = async (sess: SourceChatSession) => {
    setSelectedSession(sess)
    localStorage.setItem(SESSION_STORAGE_SESSION_KEY, sess.id)
    const src = selectedSource()
    if (src) {
      await loadMessages(src.id, sess.id)
    }
  }

  const handleCreateSession = async () => {
    const src = selectedSource()
    if (!src) return
    try {
      const session = await sourceChatApi.createSession(src.id, {})
      setSessions(prev => [session, ...prev])
      setSelectedSession(session)
      setMessages([])
    } catch {
      // 401 handled by interceptor
    }
  }

  const handleSend = async (text: string, attachments?: string[]) => {
    const src = selectedSource()
    const sess = selectedSession()
    if (!src || !sess || isStreaming()) return

    const userMsg: ChatMessage = {
      id: `tmp-${Date.now()}`,
      type: 'human',
      content: text,
    }
    const aiMsg: ChatMessage = {
      id: `tmp-ai-${Date.now()}`,
      type: 'ai',
      content: '',
    }
    setMessages(prev => [...prev, userMsg, aiMsg])
    setIsStreaming(true)

    try {
      await streamSse(
        `/api/sources/${src.id}/chat/sessions/${sess.id}/messages`,
        { message: text, attachments },
        (evt) => {
          let payload: any
          try {
            payload = JSON.parse(evt.data)
          } catch {
            return
          }
          if (payload.type === 'ai_message' || payload.type === 'ai') {
            const content = payload.content ?? payload.data?.content ?? ''
            setMessages(prev =>
              prev.map(m =>
                m.id === aiMsg.id
                  ? {
                      ...m,
                      content: (m.content || '') + content,
                      thinking: payload.thinking ?? m.thinking,
                    }
                  : m,
              ),
            )
          } else if (payload.type === 'complete') {
            void loadMessages(src.id, sess.id)
          } else if (payload.type === 'error') {
            setError(payload.message || 'Stream error')
          }
        },
      )
    } catch {
      setError('Stream failed')
    } finally {
      setIsStreaming(false)
    }
  }

  const handleCreateSite = async () => {
    const url = newSiteUrl().trim()
    if (!url) return
    try {
      const data: CreateSiteRequest = { url, name: newSiteName().trim() || undefined }
      const site = await siteApi.create(data)
      setSites(prev => [site, ...prev])
      setNewSiteUrl('')
      setNewSiteName('')
      setSiteError('')
    } catch (e) {
      setSiteError(String(e))
    }
  }

  const handleDeleteSite = async (id: string) => {
    if (!confirm('Delete this site?')) return
    try {
      await siteApi.delete(id)
      setSites(prev => prev.filter(s => s.id !== id))
      if (selectedSite()?.id === id) setSelectedSite(null)
    } catch (e) {
      setSiteError(String(e))
    }
  }

  const handleCrawlSite = async (site: Site) => {
    try {
      await siteApi.crawl(site.id, { embed: true })
      setSiteError('')
      await loadSites()
    } catch (e) {
      setSiteError(String(e))
    }
  }

  const handleSelectSite = (site: Site) => {
    setSelectedSite(site)
  }

  return (
    <div class="flex h-full flex-col gap-4">
      {/* Tab switcher */}
      <div class="flex-shrink-0 px-4 py-2 border-b border-border flex gap-4">
        <button
          onClick={() => setActiveTab('sources')}
          classList={{
            'px-2 py-1 text-sm font-medium transition-colors': true,
            'text-accent border-b-2 border-accent': activeTab() === 'sources',
            'text-muted-foreground hover:bg-muted': activeTab() !== 'sources',
          }}
        >
          {t('navigation.sources')}
        </button>
        <button
          onClick={() => setActiveTab('sites')}
          classList={{
            'px-2 py-1 text-sm font-medium transition-colors': true,
            'text-accent border-b-2 border-accent': activeTab() === 'sites',
            'text-muted-foreground hover:bg-muted': activeTab() !== 'sites',
          }}
        >
          Sites
        </button>
      </div>

      <Show when={activeTab() === 'sources'}>
        <div class="flex h-full gap-4 flex-1 min-w-0">
          <div class="w-60 flex-shrink-0 flex flex-col gap-2">
            <h2 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2">
              {t('navigation.sources')}
            </h2>
            <Show when={loading()}>
              <p class="text-xs text-muted-foreground">{t('common.loading')}</p>
            </Show>
            <Show when={error()}>
              <p class="text-xs text-destructive">{error()}</p>
            </Show>
            <div class="flex-1 overflow-y-auto space-y-1">
              <For each={sources()}>
                {(src) => (
                  <button
                    onClick={() => void loadSessions(src)}
                    classList={{
                      'w-full text-left px-3 py-2 rounded-md text-sm transition-all duration-150': true,
                      'bg-accent/10 text-accent border-r-2 border-accent': selectedSource()?.id === src.id,
                      'border-transparent hover:bg-muted': selectedSource()?.id !== src.id,
                    }}
                  >
                    <div class="truncate">{src.title}</div>
                  </button>
                )}
              </For>
            </div>
          </div>

          <div class="w-60 flex-shrink-0 flex flex-col gap-2">
            <div class="flex items-center justify-between">
              <h2 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Sessions
              </h2>
              <button
                onClick={() => void handleCreateSession()}
                class="text-xs btn-ghost px-2 py-1"
                disabled={!selectedSource()}
              >
                {t('common.new')}
              </button>
            </div>
            <div class="flex-1 overflow-y-auto space-y-1">
              <For each={sessions()}>
                {(sess) => (
                  <button
                    onClick={() => void handleSelectSession(sess)}
                    classList={{
                      'w-full text-left px-3 py-2 rounded-md text-sm transition-all duration-150': true,
                      'bg-accent/10 text-accent border-r-2 border-accent': selectedSession()?.id === sess.id,
                      'border-transparent hover:bg-muted': selectedSession()?.id !== sess.id,
                    }}
                  >
                    <div class="truncate">{sess.title || 'Untitled'}</div>
                  </button>
                )}
              </For>
            </div>
          </div>

          <div class="flex-1 min-w-0">
            <Show
              when={selectedSession()}
              fallback={
                <div class="empty-state h-full flex items-center justify-center text-muted-foreground">
                  {selectedSource() ? t('chat.startConversation') : t('common.loading')}
                </div>
              }
            >
              <ChatPanel
                sessionId={selectedSession()!.id}
                messages={messages()}
                isStreaming={isStreaming()}
                onSendMessage={handleSend}
              />
            </Show>
          </div>
        </div>
      </Show>

      <Show when={activeTab() === 'sites'}>
        <div class="flex h-full flex-col gap-4 flex-1 min-w-0">
          <div class="w-60 flex-shrink-0 flex flex-col gap-2">
            <h2 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2">
              Sites
            </h2>
            <Show when={sitesLoading()}>
              <p class="text-xs text-muted-foreground">{t('common.loading')}</p>
            </Show>
            <Show when={siteError()}>
              <p class="text-xs text-destructive">{siteError()}</p>
            </Show>
            <div class="flex-1 overflow-y-auto space-y-1">
              <For each={sites()}>
                {(site) => (
                  <button
                    onClick={() => handleSelectSite(site)}
                    classList={{
                      'w-full text-left px-3 py-2 rounded-md text-sm transition-all duration-150': true,
                      'bg-accent/10 text-accent border-r-2 border-accent': selectedSite()?.id === site.id,
                      'border-transparent hover:bg-muted': selectedSite()?.id !== site.id,
                    }}
                  >
                    <div class="flex items-center gap-2">
                      <div class="truncate">{site.name || site.url}</div>
                      <span class="text-[10px] text-muted-foreground">{site.status || 'idle'}</span>
                    </div>
                  </button>
                )}
              </For>
              <Show when={sites().length === 0}>
                <p class="text-xs text-muted-foreground text-center py-4">No sites yet</p>
              </Show>
            </div>
          </div>

          <div class="flex-1 min-w-0 flex flex-col gap-2">
            <div class="px-4 py-3 border-b border-border">
              <h2 class="text-base font-semibold mb-3">Add Site</h2>
              <div class="space-y-3">
                <div>
                  <label class="label block text-sm mb-1">Website URL *</label>
                  <input
                    class="input text-sm"
                    value={newSiteUrl()}
                    onInput={(e) => setNewSiteUrl(e.currentTarget.value)}
                    placeholder="https://example.com"
                  />
                </div>
                <div>
                  <label class="label block text-sm mb-1">Name (optional)</label>
                  <input
                    class="input text-sm"
                    value={newSiteName()}
                    onInput={(e) => setNewSiteName(e.currentTarget.value)}
                    placeholder="Display name"
                  />
                </div>
                <button
                  class="btn w-full"
                  onClick={() => void handleCreateSite()}
                  disabled={!newSiteUrl().trim()}
                >
                  Add Site
                </button>
              </div>
            </div>

            <div class="flex-1 overflow-y-auto p-4">
              <Show when={selectedSite()}>
                <div class="space-y-4">
                  <h2 class="text-base font-semibold">{selectedSite()!.name || selectedSite()!.url}</h2>
                  <div class="grid grid-cols-2 gap-2 text-sm">
                    <div class="p-3 rounded-md bg-muted">
                      <div class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                        URL
                      </div>
                      <div class="truncate text-sm">{selectedSite()!.url}</div>
                    </div>
                    <div class="p-3 rounded-md bg-muted">
                      <div class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                        Status
                      </div>
                      <div class="text-sm">{selectedSite()!.status || 'idle'}</div>
                    </div>
                    <div class="p-3 rounded-md bg-muted">
                      <div class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                        Sources
                      </div>
                      <div class="text-sm">{selectedSite()!.source_count ?? 0}</div>
                    </div>
                    <div class="p-3 rounded-md bg-muted">
                      <div class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                        Max Pages
                      </div>
                      <div class="text-sm">{selectedSite()!.max_pages}</div>
                    </div>
                  </div>
                  <div class="flex gap-2">
                    <button
                      class="btn flex-1"
                      onClick={() => void handleCrawlSite(selectedSite()!)}
                    >
                      Crawl Site
                    </button>
                    <button
                      class="btn-ghost flex-1 text-destructive"
                      onClick={() => void handleDeleteSite(selectedSite()!.id)}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              </Show>
              <Show when={!selectedSite()}>
                <div class="empty-state h-full flex items-center justify-center text-muted-foreground">
                  <p class="text-sm">Select a site to manage</p>
                </div>
              </Show>
            </div>
          </div>
        </div>
      </Show>
    </div>
  )
}