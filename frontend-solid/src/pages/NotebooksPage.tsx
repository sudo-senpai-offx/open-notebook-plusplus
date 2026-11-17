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
      <h1 class="text-2xl font-bold mb-4">{t('navigation.notebooks')}</h1>

      <div class="card p-4 mb-4">
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
            {t('common.create')}
          </button>
        </div>
      </div>

      <Show when={loading()}>
        <p class="text-sm text-muted-foreground">{t('common.loading')}</p>
      </Show>

      <div class="space-y-2">
        <For each={notebooks()}>
          {(nb) => (
            <div class="card p-4 flex items-center justify-between">
              <div>
                <div class="font-medium">{nb.name}</div>
              </div>
              <button
                class="btn-ghost text-xs"
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