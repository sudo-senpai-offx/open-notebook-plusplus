import { createSignal, onMount, Show, For } from 'solid-js'
import { t } from '@/lib/i18n'
import { attachmentApi } from '@/lib/api/settings'
import type { AttachmentRecord } from '@/lib/types/api'

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
}

export function AttachmentsPage() {
  const [attachments, setAttachments] = createSignal<AttachmentRecord[]>([])
  const [loading, setLoading] = createSignal(true)
  const [readerId, setReaderId] = createSignal<string | null>(null)
  const [readerContent, setReaderContent] = createSignal<string | null>(null)
  const [readerTitle, setReaderTitle] = createSignal('')
  const [renamingId, setRenamingId] = createSignal<string | null>(null)
  const [renameValue, setRenameValue] = createSignal('')

  const load = async () => {
    setLoading(true)
    try {
      const data = await attachmentApi.list()
      setAttachments(data.attachments || [])
    } finally {
      setLoading(false)
    }
  }

  onMount(() => void load())

  const openReader = async (att: AttachmentRecord) => {
    setReaderId(att.id)
    setReaderTitle(att.filename)
    setReaderContent('')
    try {
      const data = await attachmentApi.getContent(att.id)
      setReaderContent(data.content)
    } catch {
      setReaderContent('Unable to load content.')
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await attachmentApi.delete(id)
      setAttachments(prev => prev.filter(a => a.id !== id))
    } catch {
      // surfaced by interceptor
    }
  }

  const startRename = (att: AttachmentRecord) => {
    setRenamingId(att.id)
    setRenameValue(att.filename)
  }

  const commitRename = async (id: string) => {
    const name = renameValue().trim()
    if (!name) {
      setRenamingId(null)
      return
    }
    try {
      await attachmentApi.rename(id, name)
      setAttachments(prev =>
        prev.map(a => (a.id === id ? { ...a, filename: name } : a)),
      )
    } finally {
      setRenamingId(null)
    }
  }

  return (
    <div class="max-w-3xl">
      <h1 class="text-2xl font-bold mb-4">{t('navigation.attachments')}</h1>

      <Show when={loading()}>
        <p class="text-sm text-muted-foreground">{t('common.loading')}</p>
      </Show>

      <div class="space-y-2">
        <For each={attachments()}>
          {(att) => (
            <div class="card p-3 flex items-center justify-between gap-3">
              <div class="min-w-0 flex-1">
                <Show when={renamingId() === att.id}>
                  <input
                    class="input text-sm"
                    value={renameValue()}
                    onInput={(e) => setRenameValue(e.currentTarget.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void commitRename(att.id)
                      if (e.key === 'Escape') setRenamingId(null)
                    }}
                    onBlur={() => void commitRename(att.id)}
                  />
                </Show>
                <Show when={renamingId() !== att.id}>
                  <button
                    class="font-medium text-sm truncate text-left hover:text-accent transition-colors"
                    onClick={() => void openReader(att)}
                    title={att.filename}
                  >
                    {att.filename}
                  </button>
                  <div class="text-xs text-muted-foreground">
                    {formatBytes(att.file_size)} · {att.mime_type}
                  </div>
                </Show>
              </div>
              <div class="flex items-center gap-1 flex-shrink-0">
                <button
                  class="btn-ghost text-xs px-2 py-1"
                  onClick={() => startRename(att)}
                >
                  {t('common.edit')}
                </button>
                <button
                  class="btn-ghost text-xs px-2 py-1 text-destructive"
                  onClick={() => void handleDelete(att.id)}
                >
                  {t('common.delete')}
                </button>
              </div>
            </div>
          )}
        </For>
        <Show when={!loading() && attachments().length === 0}>
          <p class="text-sm text-muted-foreground">
            {t('common.noResults')}
          </p>
        </Show>
      </div>

      {/* Reader overlay */}
      <Show when={readerId()}>
        <div
          class="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
          onClick={() => setReaderId(null)}
        >
          <div
            class="card max-w-2xl w-full max-h-[80vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div class="flex items-center justify-between p-4 border-b border-border">
              <h2 class="font-semibold truncate">{readerTitle()}</h2>
              <button
                class="btn-ghost text-xs px-2 py-1"
                onClick={() => setReaderId(null)}
              >
                {t('common.close')}
              </button>
            </div>
            <div class="p-4 overflow-y-auto text-sm whitespace-pre-wrap break-words">
              {readerContent()}
            </div>
          </div>
        </div>
      </Show>
    </div>
  )
}