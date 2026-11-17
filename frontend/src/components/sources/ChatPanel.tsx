'use client'

import { memo, useCallback, useState, useRef, useEffect, useId } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import {
  Bot,
  User,
  Send,
  Loader2,
  FileText,
  Lightbulb,
  StickyNote,
  Clock,
  Paperclip,
  Upload,
  ExternalLink,
  Edit,
  RotateCw,
  Scroll,
} from 'lucide-react'
import { MarkdownRenderer } from '@/components/ui/markdown-renderer'
import {
  SourceChatMessage,
  SourceChatContextIndicator,
  BaseChatSession,
  HistorySelection,
  AttachmentRecord,
} from '@/lib/types/api'
import { ModelSelector } from './ModelSelector'
import { ContextIndicator } from '@/components/common/ContextIndicator'
import { SessionManager } from '@/components/sources/SessionManager'
import { MessageActions } from '@/components/sources/MessageActions'
import { convertReferencesToCompactMarkdown, createCompactReferenceLinkComponent } from '@/lib/utils/source-references'
import { useModalManager } from '@/lib/hooks/use-modal-manager'
import { toast } from 'sonner'
import { useTranslation } from '@/lib/hooks/use-translation'
import { attachmentApi, chatApi } from '@/lib/api/settings'
import { AttachmentReaderOverlay } from './AttachmentReaderOverlay'

interface NotebookContextStats {
  sourcesInsights: number
  sourcesFull: number
  notesCount: number
  tokenCount?: number
  charCount?: number
}

type HistoryRange = 'all' | '24h' | '7d'

// Approximate token count: ~4 chars per token (rule of thumb for English/mixed text)
function countTokens(text: string): number {
  if (!text) return 0
  return Math.ceil(text.length / 4)
}

interface ChatPanelProps {
  messages: SourceChatMessage[]
  isStreaming: boolean
  contextIndicators: SourceChatContextIndicator | null
  onSendMessage: (message: string, modelOverride?: string, historySince?: string, attachments?: File[]) => void
  modelOverride?: string
  onModelChange?: (model?: string) => void
  historySelection?: HistorySelection | null
  // Session management props
  sessions?: BaseChatSession[]
  currentSessionId?: string | null
  onCreateSession?: (title: string) => void
  onSelectSession?: (sessionId: string) => void
  onDeleteSession?: (sessionId: string) => void
  onUpdateSession?: (sessionId: string, title: string) => void
  loadingSessions?: boolean
  // Generic props for reusability
  title?: string
  contextType?: 'source' | 'notebook'
  // Notebook context stats (for notebook chat)
  notebookContextStats?: NotebookContextStats
  // Notebook ID for saving notes
  notebookId?: string
  // Callback when messages change due to edit (parent refreshes its list)
  onMessagesChanged?: () => void
}

export function ChatPanel({
  messages,
  isStreaming,
  contextIndicators,
  onSendMessage,
  modelOverride,
  onModelChange,
  sessions = [],
  currentSessionId,
  onCreateSession,
  onSelectSession,
  onDeleteSession,
  onUpdateSession,
  loadingSessions = false,
  title,
  contextType = 'source',
  notebookContextStats,
  notebookId,
  onMessagesChanged,
  historySelection = null
}: ChatPanelProps) {
  const { t } = useTranslation()
  const [sessionManagerOpen, setSessionManagerOpen] = useState(false)
  const [readerOpen, setReaderOpen] = useState(false)
  const [attachments, setAttachments] = useState<AttachmentRecord[]>([])
  const [attachmentsLoading, setAttachmentsLoading] = useState(false)
  const [editDialogOpen, setEditDialogOpen] = useState(false)
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)
  const [editContent, setEditContent] = useState('')
  const [editLoading, setEditLoading] = useState(false)
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)

  // Fetch all attachments for the reader overlay
  useEffect(() => {
    const fetchAttachments = async () => {
      setAttachmentsLoading(true)
      try {
        const data = await attachmentApi.list()
        setAttachments(data.attachments)
      } catch {
        // Ignore — attachments are optional
      } finally {
        setAttachmentsLoading(false)
      }
    }
    fetchAttachments()
  }, [])
  // Which slice of the conversation the next turn sends. Undefined means
  // the whole history up to the context budget; the shorthands narrow it
  // to a timeframe and travel as history_since on the send.
  const [historyRange, setHistoryRange] = useState<HistoryRange>('all')
  const historySince = historyRange === 'all' ? undefined : historyRange
  const scrollAreaRef = useRef<HTMLDivElement>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const { openModal } = useModalManager()

  // Stable reference-click handler so memoized messages don't re-render on
  // composer keystrokes (which no longer re-render this component at all, since
  // the input state lives in the ChatComposer child).
  const handleReferenceClick = useCallback((type: string, id: string) => {
    const modalType = type === 'source_insight' ? 'insight' : type as 'source' | 'note' | 'insight'

    try {
      openModal(modalType, id)
      // Note: The modal system uses URL parameters and doesn't throw errors for missing items.
      // The modal component itself will handle displaying "not found" states.
      // This try-catch is here for future enhancements or unexpected errors.
    } catch {
      toast.error(t('common.noResults'))
    }
  }, [openModal, t])

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Track scroll position to show/hide the scroll-to-bottom button
  const handleScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const el = event.currentTarget
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 20
    setShowScrollToBottom(!atBottom)
  }

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    setShowScrollToBottom(false)
  }

  // Open the edit dialog for a human message
  const handleEditClick = (message: SourceChatMessage) => {
    setEditingMessageId(message.id)
    setEditContent(message.content)
    setEditDialogOpen(true)
  }

  // Submit the edited message via the edit API
  const handleSubmitEdit = async () => {
    if (!editingMessageId || !currentSessionId || !onMessagesChanged) return
    setEditLoading(true)
    try {
      await chatApi.editMessage(currentSessionId, editingMessageId, {
        message_id: editingMessageId,
        new_content: editContent,
      })
      setEditDialogOpen(false)
      setEditingMessageId(null)
      setEditContent('')
      onMessagesChanged()
    } catch {
      toast.error(t('common.error'))
    } finally {
      setEditLoading(false)
    }
  }

  return (
    <>
    <Card className="flex flex-col h-full flex-1 overflow-hidden">
      <CardHeader className="pb-3 flex-shrink-0">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.13em] text-muted-foreground">
            <span aria-hidden className="h-3.5 w-[3px] rounded-full bg-teal" />
            {title || (contextType === 'source' ? t('chat.chatWith', { name: t('navigation.sources') }) : t('chat.chatWith', { name: t('common.notebook') }))}
          </CardTitle>
          {onSelectSession && onCreateSession && onDeleteSession && (
            <Dialog open={sessionManagerOpen} onOpenChange={setSessionManagerOpen}>
              <Button
                variant="ghost"
                size="sm"
                className="gap-2 text-muted-foreground"
                onClick={() => setSessionManagerOpen(true)}
                disabled={loadingSessions}
              >
                <Clock className="h-4 w-4" />
                <span className="text-xs">{t('chat.sessions')}</span>
              </Button>
              <DialogContent className="sm:max-w-[420px] p-0 overflow-hidden">
                <DialogTitle className="sr-only">{t('chat.sessionsTitle')}</DialogTitle>
                <SessionManager
                  sessions={sessions}
                  currentSessionId={currentSessionId ?? null}
                  onCreateSession={(title) => onCreateSession?.(title)}
                  onSelectSession={(sessionId) => {
                    onSelectSession(sessionId)
                    setSessionManagerOpen(false)
                  }}
                  onUpdateSession={(sessionId, title) => onUpdateSession?.(sessionId, title)}
                  onDeleteSession={(sessionId) => onDeleteSession?.(sessionId)}
                  loadingSessions={loadingSessions}
                />
              </DialogContent>
            </Dialog>
          )}
          {attachments.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-2 text-muted-foreground"
              onClick={() => setReaderOpen(true)}
              disabled={attachmentsLoading}
            >
              <ExternalLink className="h-4 w-4" />
              <span className="text-xs">{t('chat.attachments')}</span>
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex-1 flex flex-col min-h-0 p-0">
        <ScrollArea className="flex-1 min-h-0 px-4" ref={scrollAreaRef} onScroll={handleScroll}>
          <div className="space-y-4 py-4">
            {messages.length === 0 ? (
              <div className="text-center text-muted-foreground py-8">
                <Bot className="h-12 w-12 mx-auto mb-4 opacity-50" />
                <p className="text-sm">
                  {t('chat.startConversation', { type: contextType === 'source' ? t('navigation.sources') : t('common.notebook') })}
                </p>
                <p className="text-xs mt-2">{t('chat.askQuestions')}</p>
              </div>
            ) : (
              messages.map((message) => (
                <ChatMessage
                  key={message.id}
                  message={message}
                  notebookId={notebookId}
                  onReferenceClick={handleReferenceClick}
                  onEditClick={message.type === 'human' ? handleEditClick : undefined}
                />
              ))
            )}
            {isStreaming && (
              <div className="flex gap-3 justify-start">
                <div className="flex-shrink-0">
                  <div className="h-8 w-8 rounded-full bg-teal-tint flex items-center justify-center">
                    <Bot className="h-4 w-4 text-teal" />
                  </div>
                </div>
                <div className="rounded-lg px-4 py-2 bg-card border">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        </ScrollArea>

        {/* Scroll to bottom floating button */}
        {showScrollToBottom && (
          <Button
            onClick={scrollToBottom}
            variant="outline"
            size="icon"
            className="fixed bottom-6 right-6 z-50 h-10 w-10 rounded-full shadow-lg"
            aria-label="Scroll to bottom"
          >
            <Scroll className="h-5 w-5" />
          </Button>
        )}

        {/* Context Indicators */}
        {contextIndicators && (
          <div className="border-t px-4 py-2">
            <div className="flex flex-wrap gap-2 text-xs">
              {contextIndicators.sources?.length > 0 && (
                <Badge variant="outline" className="gap-1">
                  <FileText className="h-3 w-3" />
                  {contextIndicators.sources.length} {t('navigation.sources')}
                </Badge>
              )}
              {contextIndicators.insights?.length > 0 && (
                <Badge variant="outline" className="gap-1">
                  <Lightbulb className="h-3 w-3" />
                  {contextIndicators.insights.length} {contextIndicators.insights.length === 1 ? t('common.insight') : t('common.insights')}
                </Badge>
              )}
              {contextIndicators.notes?.length > 0 && (
                <Badge variant="outline" className="gap-1">
                  <StickyNote className="h-3 w-3" />
                  {contextIndicators.notes.length} {contextIndicators.notes.length === 1 ? t('common.note') : t('common.notes')}
                </Badge>
              )}
            </div>
          </div>
        )}

        {/* Notebook Context Indicator */}
        {notebookContextStats && (
          <ContextIndicator
            sourcesInsights={notebookContextStats.sourcesInsights}
            sourcesFull={notebookContextStats.sourcesFull}
            notesCount={notebookContextStats.notesCount}
            tokenCount={notebookContextStats.tokenCount}
            charCount={notebookContextStats.charCount}
          />
        )}

        {/* History window: what the model saw last turn, and the range
            for the next one. The range chips always show - a control
            that appears only after trimming would be undiscoverable
            exactly when a conversation grows long. The "last N of M"
            text appears only when a turn actually trimmed something. */}
        <div className="border-t px-4 py-2 flex items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">
            {historySelection && historySelection.dropped_exchanges > 0
              ? t('chat.historyWindow', {
                  kept: historySelection.kept_exchanges,
                  total: historySelection.total_exchanges
                })
              : null}
          </span>
          <div className="flex gap-1">
            {(['all', '24h', '7d'] as HistoryRange[]).map((range) => (
              <Button
                key={range}
                size="sm"
                variant={historyRange === range ? 'secondary' : 'ghost'}
                className="h-6 px-2 text-xs"
                onClick={() => setHistoryRange(range)}
                disabled={isStreaming}
              >
                {range === 'all' ? t('common.all') : range}
              </Button>
            ))}
          </div>
        </div>

        {/* Token counters: message+attachments, reasoning, response */}
        {messages.length > 0 && (
          <div className="border-t px-4 py-2 flex items-center gap-4 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <FileText className="h-3 w-3" />
              {t('chat.tokenCountMessage')}: {countTokens(messages.map(m => m.content).join(' '))}
            </span>
            {messages.some(m => m.type === 'ai' && m.thinking) && (
              <span className="flex items-center gap-1">
                <Lightbulb className="h-3 w-3" />
                {t('chat.tokenCountReasoning')}: {countTokens(messages.filter(m => m.type === 'ai' && m.thinking).map(m => m.thinking || '').join(' '))}
              </span>
            )}
            {messages.some(m => m.type === 'ai') && (
              <span className="flex items-center gap-1">
                <Bot className="h-3 w-3" />
                {t('chat.tokenCountResponse')}: {countTokens(messages.filter(m => m.type === 'ai').map(m => m.content).join(' '))}
              </span>
            )}
          </div>
        )}

        {/* Input Area */}
        <ChatComposer
          onSendMessage={(message: string, modelOverride?: string, historySince?: string, attachments?: File[]) =>
            onSendMessage(message, modelOverride, historySince, attachments)
          }
          isStreaming={isStreaming}
          modelOverride={modelOverride}
          onModelChange={onModelChange}
          historySince={historySince}
        />
      </CardContent>
    </Card>

    <AttachmentReaderOverlay
      attachments={attachments}
      open={readerOpen}
      onOpenChange={setReaderOpen}
    />

    {/* Edit message dialog */}
    <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle className="text-base">{t('common.edit')} {t('chat.message')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <Textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            rows={5}
            className="resize-none"
            disabled={editLoading}
          />
          <p className="text-xs text-muted-foreground">
            {t('chat.editMessageHint')}
          </p>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => setEditDialogOpen(false)}
            disabled={editLoading}
          >
            {t('common.cancel')}
          </Button>
          <Button
            onClick={handleSubmitEdit}
            disabled={editLoading || !editContent.trim()}
          >
            {editLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Edit className="h-4 w-4" />
            )}
            {t('common.edit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    </>
  )
}

// Composer owns the input state so keystrokes (including IME composition) only
// re-render this small component instead of the whole message history.
interface ChatComposerProps {
  onSendMessage: (message: string, modelOverride?: string, historySince?: string, attachments?: File[]) => void
  isStreaming: boolean
  modelOverride?: string
  onModelChange?: (model?: string) => void
  historySince?: string
}

function ChatComposer({
  onSendMessage,
  isStreaming,
  modelOverride,
  onModelChange,
  historySince
}: ChatComposerProps) {
  const { t } = useTranslation()
  const chatInputId = useId()
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<File[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleSend = () => {
    if (input.trim() && !isStreaming) {
      onSendMessage(input.trim(), modelOverride, historySince, attachments.length > 0 ? attachments : undefined)
      setInput('')
      setAttachments([])
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    }
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files) {
      setAttachments(Array.from(files))
    }
  }

  // Extract files from clipboard paste event (e.g. screenshot pasted, file copied)
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items || []
    const files: File[] = []
    for (const item of items) {
      if (item.kind === 'file' && item.type) {
        const file = item.getAsFile()
        if (file) {
          files.push(file)
        }
      }
    }
    if (files.length > 0) {
      setAttachments(prev => [...prev, ...files])
    }
  }

  // Handle drag-and-drop file paste
  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    const files = Array.from(e.dataTransfer?.files || [])
    if (files.length > 0) {
      setAttachments(prev => [...prev, ...files])
    }
  }

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
  }

  const removeAttachment = (index: number) => {
    setAttachments(attachments.filter((_, i) => i !== index))
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Detect platform for correct modifier key
    const isMac = typeof navigator !== 'undefined' && navigator.userAgent.toUpperCase().indexOf('MAC') >= 0
    const isModifierPressed = isMac ? e.metaKey : e.ctrlKey

    if (e.key === 'Enter' && isModifierPressed) {
      e.preventDefault()
      handleSend()
    }
  }

  // Detect platform for placeholder text
  const isMac = typeof navigator !== 'undefined' && navigator.userAgent.toUpperCase().indexOf('MAC') >= 0
  const keyHint = isMac ? '⌘+Enter' : 'Ctrl+Enter'

  return (
    <div
      className="flex-shrink-0 p-4 space-y-3 border-t"
      onDrop={handleDrop}
      onDragOver={handleDragOver}
    >
      {/* Model selector */}
      {onModelChange && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">{t('chat.model')}</span>
          <ModelSelector
            currentModel={modelOverride}
            onModelChange={onModelChange}
            disabled={isStreaming}
          />
        </div>
      )}

      {/* Attachments preview */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 max-w-full">
          {attachments.map((file, idx) => (
            <div key={idx} className="flex items-center gap-2 px-2 py-1 bg-muted rounded border border-border">
              <Paperclip className="h-3 w-3 text-muted-foreground" />
              <span className="text-xs text-muted-foreground truncate max-w-[120px]">{file.name}</span>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 p-0"
                onClick={() => removeAttachment(idx)}
                aria-label="Remove attachment"
              >
                ×
              </Button>
            </div>
          ))}
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setAttachments([])
              if (fileInputRef.current) {
                fileInputRef.current.value = ''
              }
            }}
            className="h-7 px-2 text-xs"
          >
            <Upload className="h-3 w-3" /> Clear
          </Button>
        </div>
      )}

      <div className="flex gap-2 items-end min-w-0">
        <Textarea
          id={chatInputId}
          name="chat-message"
          autoComplete="off"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={`${t('chat.sendPlaceholder')} (${t('chat.pressToSend', { key: keyHint })})`}
          disabled={isStreaming}
          className="flex-1 min-h-[40px] max-h-[100px] resize-none py-2 px-3 min-w-0"
          rows={1}
        />
        <Button
          onClick={() => fileInputRef.current?.click()}
          disabled={isStreaming}
          size="icon"
          className="h-[40px] w-[40px] flex-shrink-0"
          aria-label="attach file"
        >
          <Paperclip className="h-4 w-4" />
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/*,.pdf,.txt,.md,.doc,.docx"
          className="hidden"
          onChange={handleFileChange}
          disabled={isStreaming}
        />
        <Button
          onClick={handleSend}
          disabled={!input.trim() || isStreaming}
          size="icon"
          className="h-[40px] w-[40px] flex-shrink-0"
          aria-label="send"
        >
          {isStreaming ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Send className="h-4 w-4" />
          )}
        </Button>
      </div>
    </div>
  )
}

// Single chat message row. Memoized so historical messages don't re-render when
// unrelated state (e.g. the composer input) changes.
interface ChatMessageProps {
  message: SourceChatMessage
  notebookId?: string
  onReferenceClick: (type: string, id: string) => void
  onEditClick?: (message: SourceChatMessage) => void
}

const ChatMessage = memo(function ChatMessage({
  message,
  notebookId,
  onReferenceClick,
  onEditClick
}: ChatMessageProps) {
  const { t } = useTranslation()
  return (
    <div
      className={`flex gap-3 ${
        message.type === 'human' ? 'justify-end' : 'justify-start'
      }`}
    >
      {message.type === 'ai' && (
        <div className="flex-shrink-0">
          <div className="h-8 w-8 rounded-full bg-teal-tint flex items-center justify-center">
            <Bot className="h-4 w-4 text-teal" />
          </div>
        </div>
      )}
      <div className="flex flex-col gap-2 max-w-[80%]">
        <div
          className={`rounded-lg px-4 py-2 border ${
            message.type === 'human'
              ? 'bg-muted'
              : 'bg-card'
          }`}
        >
          {message.type === 'ai' ? (
            <AIMessageContent
              content={message.content}
              thinking={message.thinking ?? undefined}
              onReferenceClick={onReferenceClick}
            />
          ) : (
            <>
              <p className="text-sm break-all">{message.content}</p>
              {message.edit_count && message.edit_count > 0 && (
                <div className="flex items-center gap-1 mt-1">
                  <RotateCw className="h-3 w-3 text-muted-foreground" />
                  <span className="text-[10px] text-muted-foreground">
                    Edit {message.edit_count}/4
                  </span>
                </div>
              )}
              {onEditClick && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onEditClick(message)}
                  className="w-fit self-end mt-1 px-2 py-1 text-xs"
                >
                  <Edit className="h-3 w-3" /> {t('common.edit')}
                </Button>
              )}
            </>
          )}
        </div>
        {message.type === 'ai' && (
          <MessageActions
            content={message.content}
            notebookId={notebookId}
          />
        )}
      </div>
      {message.type === 'human' && (
        <div className="flex-shrink-0">
          <div className="h-8 w-8 rounded-full bg-muted border flex items-center justify-center">
            <User className="h-4 w-4 text-muted-foreground" />
          </div>
        </div>
      )}
    </div>
  )
})

// Helper component to render AI messages with clickable references
function AIMessageContent({
  content,
  thinking,
  onReferenceClick
}: {
  content: string
  thinking?: string
  onReferenceClick: (type: string, id: string) => void
}) {
  const { t } = useTranslation()
  const [thinkingExpanded, setThinkingExpanded] = useState(false)
  // Convert references to compact markdown with numbered citations
  const markdownWithCompactRefs = convertReferencesToCompactMarkdown(content, t('common.references'))

  // Create custom link component for compact references
  const LinkComponent = createCompactReferenceLinkComponent(onReferenceClick)

  return (
    <>
      <MarkdownRenderer components={{
        a: LinkComponent
      }}>
        {markdownWithCompactRefs}
      </MarkdownRenderer>
      {thinking && (
        <div className="mt-2 pt-2 border-t border-border/50">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setThinkingExpanded(!thinkingExpanded)}
            className="w-full text-left text-xs text-muted-foreground hover:text-foreground"
          >
            {thinkingExpanded ? '▾' : '▸'} {t('chat.reasoningTokens')}
          </Button>
          {thinkingExpanded && (
            <div className="mt-2 p-3 bg-muted/50 rounded text-xs text-muted-foreground whitespace-pre-wrap break-words max-h-[300px] overflow-y-auto">
              {thinking}
            </div>
          )}
        </div>
      )}
    </>
  )
}
