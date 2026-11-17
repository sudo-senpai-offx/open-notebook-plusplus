import { createSignal, Show, For } from 'solid-js'
import { t } from '@/lib/i18n'
import {
  Bot, User, Send, Loader2, FileText, Lightbulb,
  RotateCw, Scroll,
} from 'lucide-solid'
import type { ChatMessage } from '@/lib/types/api'

export interface ChatPanelProps {
  sessionId: string
  messages: ChatMessage[]
  isStreaming: boolean
  onSendMessage: (message: string, attachments?: File[]) => void
  modelOverride?: string
  historySelection?: { kept_exchanges: number; total_exchanges: number; reason: string } | null
}

export function ChatPanel(props: ChatPanelProps) {
  const [input, setInput] = createSignal('')
  const [showScrollToBottom, setShowScrollToBottom] = createSignal(false)

  let endEl: HTMLDivElement | undefined
  let inputRef: HTMLTextAreaElement | undefined

  const handleSend = () => {
    const text = input().trim()
    if (!text) return
    props.onSendMessage(text)
    setInput('')
    inputRef?.focus()
  }

  const handleScroll = (e: Event) => {
    const el = e.currentTarget as HTMLElement
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 20
    setShowScrollToBottom(!atBottom)
  }

  const scrollToBottom = () => {
    endEl?.scrollIntoView({ behavior: 'smooth' })
    setShowScrollToBottom(false)
  }

  const countTokens = (text: string): number =>
    text ? Math.ceil(text.length / 4) : 0

  return (
    <div class="flex flex-col h-full bg-card">
      <div class="flex-shrink-0 px-4 py-3 border-b border-border flex items-center justify-between">
        <span class="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('navigation.sources')}
        </span>
      </div>

      <div class="flex-1 overflow-y-auto px-4" onScroll={handleScroll}>
        <div class="space-y-4 py-4">
          <Show
            when={props.messages.length > 0}
            fallback={
              <div class="text-center py-8 text-muted-foreground">
                <Bot class="w-12 h-12 mx-auto mb-3 opacity-40" />
                <p class="text-sm">{t('chat.startConversation')}</p>
              </div>
            }
          >
            <For each={props.messages}>
              {(msg) => <ChatMessageRow message={msg} />}
            </For>
          </Show>

          <Show when={props.isStreaming}>
            <div class="flex gap-2 items-center">
              <Bot class="w-4 h-4" />
              <Loader2 class="w-4 h-4 animate-spin" />
            </div>
          </Show>

          <div ref={endEl} />
        </div>
      </div>

      <Show when={showScrollToBottom()}>
        <button
          onClick={scrollToBottom}
          class="absolute bottom-4 right-4 p-2 rounded-full bg-card border shadow-md"
        >
          <Scroll class="w-4 h-4" />
        </button>
      </Show>

      <Show when={props.messages.length > 0}>
        <div class="flex-shrink-0 px-4 py-2 border-t border-border flex items-center gap-4 text-xs text-muted-foreground">
          <span class="flex items-center gap-1">
            <FileText class="w-3 h-3" />
            {countTokens(props.messages.map(m => m.content).join(' '))} {t('chat.tokenCountMessage')}
          </span>
          <Show when={props.messages.some(m => m.type === 'ai' && m.thinking)}>
            <span class="flex items-center gap-1">
              <Lightbulb class="w-3 h-3" />
              {countTokens(props.messages.filter(m => m.type === 'ai' && m.thinking).map(m => m.thinking || '').join(' '))} {t('chat.tokenCountReasoning')}
            </span>
          </Show>
          <Show when={props.messages.some(m => m.type === 'ai')}>
            <span class="flex items-center gap-1">
              <Bot class="w-3 h-3" />
              {countTokens(props.messages.filter(m => m.type === 'ai').map(m => m.content).join(' '))} {t('chat.tokenCountResponse')}
            </span>
          </Show>
        </div>
      </Show>

      <div class="flex-shrink-0 border-t border-border p-3">
        <div class="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input()}
            onInput={(e) => setInput(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                handleSend()
              }
            }}
            class="flex-1 rounded border border-border bg-bg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent resize-none"
            rows={2}
            placeholder={t('common.search')}
          />
          <button
            onClick={handleSend}
            disabled={!input().trim()}
            class="p-2 rounded bg-accent text-white disabled:opacity-50"
          >
            <Send class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  )
}

function ChatMessageRow(props: { message: ChatMessage }) {
  const [thinkingExpanded, setThinkingExpanded] = createSignal(false)
  const isAi = () => props.message.type === 'ai'

  return (
    <div class={`flex gap-3 ${isAi() ? 'justify-start' : 'justify-end'}`}>
      <Show when={isAi()}>
        <div class="flex-shrink-0">
          <div class="w-8 h-8 rounded-full bg-accent/10 flex items-center justify-center">
            <Bot class="w-4 h-4 text-accent" />
          </div>
        </div>
      </Show>

      <div class="flex flex-col gap-1 max-w-[80%]">
        <div class={`rounded-lg px-4 py-2 border ${isAi() ? 'bg-card' : 'bg-muted'}`}>
          <Show
            when={isAi()}
            fallback={
              <p class="text-sm break-all">{props.message.content}</p>
            }
          >
            <div class="text-sm break-words whitespace-pre-wrap">{props.message.content}</div>
            <Show when={props.message.thinking}>
              <div class="mt-2 pt-2 border-t border-border/50">
                <button
                  onClick={() => setThinkingExpanded(!thinkingExpanded())}
                  class="text-left text-xs text-muted-foreground hover:text-foreground"
                >
                  {thinkingExpanded() ? '▾' : '▸'} {t('common.reasoning')}
                </button>
                <Show when={thinkingExpanded()}>
                  <div class="mt-2 p-3 bg-muted/50 rounded text-xs whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
                    {props.message.thinking}
                  </div>
                </Show>
              </div>
            </Show>
          </Show>
        </div>
        <Show when={!isAi() && (props.message.edit_count ?? 0) > 0}>
          <div class="flex items-center gap-1">
            <RotateCw class="w-3 h-3 text-muted-foreground" />
            <span class="text-[10px] text-muted-foreground">
              {props.message.edit_count}/4
            </span>
          </div>
        </Show>
      </div>

      <Show when={!isAi()}>
        <div class="flex-shrink-0">
          <div class="w-8 h-8 rounded-full bg-muted border flex items-center justify-center">
            <User class="w-4 h-4 text-muted-foreground" />
          </div>
        </div>
      </Show>
    </div>
  )
}