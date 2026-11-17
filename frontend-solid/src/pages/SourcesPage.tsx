import { createSignal, onMount, Show, For } from 'solid-js'
import { t } from '@/lib/i18n'
import { sourceApi, sourceChatApi } from '@/lib/api/settings'
import { streamSse } from '@/lib/api/sse'
import { ChatPanel } from '@/components/sources/ChatPanel'
import type { Source, SourceChatSession, ChatMessage } from '@/lib/types/api'

export function SourcesPage() {
  const [sources, setSources] = createSignal<Source[]>([])
  const [selectedSource, setSelectedSource] = createSignal<Source | null>(null)
  const [sessions, setSessions] = createSignal<SourceChatSession[]>([])
  const [selectedSession, setSelectedSession] =
    createSignal<SourceChatSession | null>(null)
  const [messages, setMessages] = createSignal<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = createSignal(false)
  const [loading, setLoading] = createSignal(true)
  const [error, setError] = createSignal('')

  onMount(async () => {
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
  })

  const loadSessions = async (src: Source) => {
    setSelectedSource(src)
    try {
      const list = await sourceChatApi.listSessions(src.id)
      setSessions(list)
      if (list.length > 0) {
        await loadMessages(src.id, list[0].id)
        setSelectedSession(list[0])
      } else {
        setSelectedSession(null)
        setMessages([])
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

  const handleSend = async (text: string) => {
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
        { message: text },
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

  return (
    <div class="flex h-full gap-4">
      <div class="w-56 flex-shrink-0 flex flex-col gap-2">
        <h2 class="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
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
                  'w-full text-left px-3 py-2 rounded text-sm transition-colors': true,
                  'bg-accent/10 text-accent': selectedSource()?.id === src.id,
                  'hover:bg-muted': selectedSource()?.id !== src.id,
                }}
              >
                <div class="truncate">{src.title}</div>
              </button>
            )}
          </For>
        </div>
      </div>

      <div class="w-56 flex-shrink-0 flex flex-col gap-2">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
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
                  'w-full text-left px-3 py-2 rounded text-sm transition-colors': true,
                  'bg-accent/10 text-accent': selectedSession()?.id === sess.id,
                  'hover:bg-muted': selectedSession()?.id !== sess.id,
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
            <div class="h-full flex items-center justify-center text-muted-foreground text-sm">
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
  )
}