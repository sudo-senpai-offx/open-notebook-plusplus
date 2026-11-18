import { createSignal, onMount, Show, For } from 'solid-js'
import { t } from '@/lib/i18n'
import { notebookApi } from '@/lib/api/settings'
import type { Notebook } from '@/lib/types/api'

export function NotebooksPage() {
  const [notebooks, setNotebooks] = createSignal<Notebook[]>([])
  const [loading, setLoading] = createSignal(true)
  const [creating, setCreating] = createSignal(false)
  const [newTitle, setNewTitle] = createSignal('')

  const load = async () => {
    try {
      const list = await notebookApi.list()
      setNotebooks(list)
    } catch {
      // 401 handled by interceptor
    } finally {
      setLoading(false)
    }
  }

  onMount(() => void load())

  const handleCreate = async () => {
    const title = newTitle().trim()
    if (!title) return
    setCreating(true)
    try {
      const nb = await notebookApi.create(title)
      setNotebooks(prev => [nb, ...prev])
      setNewTitle('')
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await notebookApi.delete(id)
      setNotebooks(prev => prev.filter(n => n.id !== id))
    } catch {
      // surfaced by interceptor
    }
  }

  return (
    <div class="max-w-3xl">
      <div class="flex items-center justify-between mb-6">
        <h1 class="text-2xl font-bold">{t('navigation.notebooks')}</h1>
      </div>

      <div class="card p-5 mb-6">
        <div class="flex gap-2">
          <input
            class="input flex-1"
            placeholder={t('common.new')}
            value={newTitle()}
            onInput={(e) => setNewTitle(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void handleCreate()
            }}
          />
          <button
            class="btn"
            onClick={() => void handleCreate()}
            disabled={creating() || !newTitle().trim()}
          >
            {creating() ? t('common.processing') : t('common.create')}
          </button>
        </div>
      </div>

      <Show when={loading()}>
        <div class="empty-state py-12">
          <span class="text-muted-foreground text-sm">Loading notebooks…</span>
        </div>
      </Show>

      <Show when={!loading() && notebooks().length === 0}>
        <div class="empty-state py-12">
          <span class="text-muted-foreground text-sm">{t('common.noResults')}</span>
        </div>
      </Show>

      <div class="space-y-1.5">
        <For each={notebooks()}>
          {(nb) => (
            <div class="card p-4 flex items-center justify-between group">
              <div class="flex items-center gap-3 cursor-pointer cursor-default">
                <div class="w-8 h-8 rounded-md bg-accent/10 flex items-center justify-center text-accent text-sm font-medium">
                  NB
                </div>
                <div class="font-medium">{nb.name}</div>
              </div>
              <button
                class="btn-ghost text-xs p-1.5 text-destructive hover:bg-destructive/10"
                onClick={() => void handleDelete(nb.id)}
              >
                {t('common.delete')}
              </button>
            </div>
          )}
        </For>
      </div>
    </div>
  )
}