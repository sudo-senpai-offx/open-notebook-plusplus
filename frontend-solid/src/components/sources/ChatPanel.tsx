import { createSignal, Show, For } from 'solid-js'
import { t } from '@/lib/i18n'
import {
  Bot, User, Send, Loader2, FileText, Lightbulb,
  RotateCw, Scroll, Paperclip, X, Upload, Camera, Clipboard,
} from 'lucide-solid'
import { attachmentApi } from '@/lib/api/settings'
import type { AttachmentRecord, ChatMessage } from '@/lib/types/api'

export interface ChatPanelProps {
  sessionId: string
  messages: ChatMessage[]
  isStreaming: boolean
  onSendMessage: (message: string, attachments?: string[]) => void
  modelOverride?: string
  historySelection?: { kept_exchanges: number; total_exchanges: number; reason: string } | null
}

export function ChatPanel(props: ChatPanelProps) {
  const [input, setInput] = createSignal('')
  const [showScrollToBottom, setShowScrollToBottom] = createSignal(false)
  const [selectedAttachments, setSelectedAttachments] = createSignal<string[]>([])
  const [attachments, setAttachments] = createSignal<AttachmentRecord[]>([])
  const [attachmentsLoading, setAttachmentsLoading] = createSignal(false)
  const [pickerOpen, setPickerOpen] = createSignal(false)
  const [uploadDialogOpen, setUploadDialogOpen] = createSignal(false)

  let endEl: HTMLDivElement | undefined
  let inputRef: HTMLTextAreaElement | undefined
  let fileInputRef: HTMLInputElement | null = null
  let cameraInputRef: HTMLInputElement | null = null

  // Load attachments on mount
  const loadAttachments = async () => {
    setAttachmentsLoading(true)
    try {
      const data = await attachmentApi.list()
      setAttachments(data.attachments || [])
    } catch {
      // attachments optional
    } finally {
      setAttachmentsLoading(false)
    }
  }

  const toggleAttachment = (filePath: string) => {
    setSelectedAttachments(prev =>
      prev.includes(filePath)
        ? prev.filter(p => p !== filePath)
        : [...prev, filePath],
    )
  }

  const isAttachmentSelected = (filePath: string) =>
    selectedAttachments().includes(filePath)

  const handleSend = () => {
    const text = input().trim()
    if (!text && selectedAttachments().length === 0) return
    props.onSendMessage(text, selectedAttachments().length > 0 ? selectedAttachments() : undefined)
    setInput('')
    setSelectedAttachments([])
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

  const formatFileSize = (bytes: number): string => {
    if (!bytes) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
  }

  // Upload via file picker
  const handleFileUpload = async (e: Event) => {
    const inputEl = e.target as HTMLInputElement
    const files = inputEl.files
    if (!files || files.length === 0) return
    setUploadDialogOpen(false)
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i]
        await attachmentApi.upload(file)
      }
      inputEl.value = ''
      await loadAttachments()
      setPickerOpen(true)
    } catch {
      // error surfaced by interceptor
    }
  }

  // Upload via camera
  const handleCameraUpload = async (e: Event) => {
    const inputEl = e.target as HTMLInputElement
    const files = inputEl.files
    if (!files || files.length === 0) return
    setUploadDialogOpen(false)
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i]
        await attachmentApi.upload(file)
      }
      inputEl.value = ''
      await loadAttachments()
      setPickerOpen(true)
    } catch {
      // error surfaced by interceptor
    }
  }

  // Paste clipboard as text attachment
  const handlePasteClipboard = async () => {
    try {
      setUploadDialogOpen(false)
      try {
        const text = await navigator.clipboard.readText()
        if (!text || !text.trim()) {
          return
        }
        const blob = new Blob([text], { type: 'text/plain' })
        const file = new File([blob], `clipboard-${Date.now()}.txt`, { type: 'text/plain' })
        await attachmentApi.upload(file)
        await loadAttachments()
        setPickerOpen(true)
      } catch {
        // Clipboard access may be denied
      }
    } catch {
      // error surfaced by interceptor
    }
  }

  return (
    <div class="flex flex-col h-full bg-card">
      <div class="flex-shrink-0 px-4 py-3 border-b border-border flex items-center justify-between">
        <span class="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t('navigation.sources')}
        </span>
        <div class="flex gap-1">
          <button
            onClick={() => { setUploadDialogOpen(true); void loadAttachments() }}
            class="text-xs btn-ghost px-2 py-1"
            disabled={attachmentsLoading()}
            title={t('common.new')}
          >
            <Paperclip class="w-3 h-3 mr-1" />
            {t('common.new')}
          </button>
          <button
            onClick={() => setPickerOpen(!pickerOpen())}
            class="text-xs btn-ghost px-2 py-1"
            disabled={attachmentsLoading() || attachments().length === 0}
          >
            <FileText class="w-3 h-3" />
          </button>
        </div>
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

      {/* Selected attachment chips */}
      <Show when={selectedAttachments().length > 0}>
        <div class="flex-shrink-0 px-4 py-2 border-b border-border flex flex-wrap gap-2">
          <For each={selectedAttachments()}>
            {(filePath) => {
              const att = attachments().find(a => a.file_path === filePath)
              return (
                <div class="flex items-center gap-1 px-2 py-1 rounded bg-accent/10 border border-accent/20">
                  <FileText class="w-3 h-3 text-accent" />
                  <span class="text-xs truncate max-w-[120px]">{att?.filename || filePath.split('/').pop()}</span>
                  <button
                    onClick={() => toggleAttachment(filePath)}
                    class="p-1 text-accent hover:bg-accent/20 rounded"
                  >
                    <X class="w-3 h-3" />
                  </button>
                </div>
              )
            }}
          </For>
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
            onClick={() => setUploadDialogOpen(!uploadDialogOpen())}
            class="p-2 rounded bg-accent text-white"
          >
            <Paperclip class="w-4 h-4" />
          </button>
          <button
            onClick={handleSend}
            disabled={!input().trim() && selectedAttachments().length === 0}
            class="p-2 rounded bg-accent text-white disabled:opacity-50"
          >
            <Send class="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Upload dialog (camera / clipboard / file) */}
      <Show when={uploadDialogOpen()}>
        <div
          class="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
          onClick={() => setUploadDialogOpen(false)}
        >
          <div
            class="card max-w-md w-full p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 class="text-lg font-bold mb-4">Add Attachment</h2>
            <div class="space-y-3">
              <button
                onClick={() => {
                  setUploadDialogOpen(false)
                  fileInputRef?.click()
                }}
                class="w-full btn-ghost text-left p-3 rounded border flex items-center justify-between hover:bg-muted"
              >
                <div class="flex items-center gap-2">
                  <Upload class="w-4 h-4" />
                  <span>Upload from device</span>
                </div>
              </button>
              <button
                onClick={() => {
                  setUploadDialogOpen(false)
                  cameraInputRef?.click()
                }}
                class="w-full btn-ghost text-left p-3 rounded border flex items-center justify-between hover:bg-muted"
              >
                <div class="flex items-center gap-2">
                  <Camera class="w-4 h-4" />
                  <span>Capture from camera</span>
                </div>
              </button>
              <button
                onClick={handlePasteClipboard}
                class="w-full btn-ghost text-left p-3 rounded border flex items-center justify-between hover:bg-muted"
              >
                <div class="flex items-center gap-2">
                  <Clipboard class="w-4 h-4" />
                  <span>Paste clipboard as text</span>
                </div>
              </button>
            </div>
          </div>
        </div>
      </Show>

      {/* Attachment picker (existing attachments) */}
      <Show when={pickerOpen()}>
        <div
          class="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
          onClick={() => setPickerOpen(false)}
        >
          <div
            class="card max-w-2xl w-full max-h-[80vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div class="flex items-center justify-between p-4 border-b border-border">
              <h2 class="font-semibold">Attachments</h2>
              <button
                class="btn-ghost text-xs px-2 py-1"
                onClick={() => setPickerOpen(false)}
              >
                {t('common.close')}
              </button>
            </div>
            <div class="p-4 overflow-y-auto">
              <Show when={attachmentsLoading()}>
                <p class="text-xs text-muted-foreground">{t('common.processing')}</p>
              </Show>
              <Show when={!attachmentsLoading() && attachments().length === 0}>
                <p class="text-xs text-muted-foreground">
                  {t('common.noResults')}
                </p>
              </Show>
              <div class="space-y-2">
                <For each={attachments()}>
                  {(att) => (
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        toggleAttachment(att.file_path)
                      }}
                      classList={{
                        'flex items-center gap-3 px-3 py-2 rounded border transition-colors': true,
                        'bg-accent/10 border-accent/50': isAttachmentSelected(att.file_path),
                        'border-border hover:bg-muted': !isAttachmentSelected(att.file_path),
                      }}
                    >
                      <FileText
                        class={`w-4 h-4 ${isAttachmentSelected(att.file_path) ? 'text-accent' : 'text-muted-foreground'}`}
                      />
                      <div class="flex-1 min-w-0">
                        <div class="text-sm truncate font-medium">{att.filename}</div>
                        <div class="text-xs text-muted-foreground truncate">
                          {att.mime_type} · {formatFileSize(att.file_size)}
                        </div>
                      </div>
                      <Show when={isAttachmentSelected(att.file_path)}>
                        <X class="w-4 h-4 text-accent" />
                      </Show>
                    </button>
                  )}
                </For>
              </div>
            </div>
          </div>
        </div>
      </Show>

      {/* Hidden file inputs */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={handleFileUpload}
        class="hidden"
        accept="*"
      />
      <input
        ref={cameraInputRef}
        type="file"
        accept="image/*"
        capture="environment"
        onChange={handleCameraUpload}
        class="hidden"
      />
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