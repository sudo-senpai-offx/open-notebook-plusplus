import { createSignal, onMount, Show, For } from 'solid-js'
import { t, setLanguage, getLanguage, LANGUAGES } from '@/lib/i18n'
import {
  settingsApi,
  browserAiApi,
  deepseekApi,
} from '@/lib/api/settings'
import type {
  BrowserAIStatus,
  DeepSeekSessionStatus,
  SettingsResponse,
  SettingsUpdate,
} from '@/lib/types/api'

export function SettingsPage() {
  const [language, setLang] = createSignal(getLanguage())
  const [saving, setSaving] = createSignal(false)
  const [saved, setSaved] = createSignal(false)
  const [settings, setSettings] = createSignal<SettingsResponse | null>(null)
  const [browserAi, setBrowserAi] = createSignal<BrowserAIStatus | null>(null)
  const [deepseek, setDeepseek] = createSignal<DeepSeekSessionStatus | null>(null)
  const [deepseekAvailable, setDeepseekAvailable] = createSignal<boolean | null>(null)
  const [editMode, setEditMode] = createSignal(false)

  // Working copy of settings for editing
  const [editValues, setEditValues] = createSignal<SettingsUpdate>({})

  const load = async () => {
    try {
      const s = await settingsApi.get()
      setSettings(s)
      setEditValues({})
    } catch {
      // settings optional
    }
    try {
      setBrowserAi(await browserAiApi.status())
    } catch {
      // ignore
    }
    try {
      setDeepseek(await deepseekApi.status())
    } catch {
      // ignore
    }
    try {
      const avail = await deepseekApi.available()
      setDeepseekAvailable(avail.available || false)
    } catch {
      setDeepseekAvailable(false)
    }
  }

  onMount(() => void load())

  const handleLanguage = async (code: string) => {
    setLang(code)
    await setLanguage(code)
  }

  const handleEditValue = (key: string, value: string | boolean | string[]) => {
    setEditValues(prev => ({ ...prev, [key]: value }))
  }

  const toggleEditMode = () => {
    setEditMode(!editMode())
    if (!editMode()) {
      setEditValues({})
    }
  }

  const handleSave = async () => {
    setSaving(true)
    setSaved(false)
    try {
      await settingsApi.update(editValues())
      setEditValues({})
      setEditMode(false)
      await load()
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch {
      // error surfaced by interceptor or toast
    } finally {
      setSaving(false)
    }
  }

  const handleDeepSeekConfigure = async () => {
    const ds = deepseek()
    if (!ds) return
    // Prompt user for credentials via inline inputs in edit mode
    setEditMode(true)
  }

  const verifyDeepSeek = async () => {
    try {
      const avail = await deepseekApi.available()
      setDeepseekAvailable(avail.available || false)
    } catch {
      setDeepseekAvailable(false)
    }
  }

  const refreshSettings = () => {
    void load()
  }

  const fieldLabel = (key: string): string => {
    const labels: Record<string, string> = {
      default_content_processing_engine_doc: 'Default Content Processing Engine (Doc)',
      default_content_processing_engine_url: 'Default Content Processing Engine (URL)',
      default_embedding_option: 'Default Embedding Option',
      auto_delete_files: 'Auto Delete Files',
      docling_ocr: 'Docling OCR',
      docling_formulas: 'Docling Formulas',
      docling_vision: 'Docling Vision',
      youtube_preferred_languages: 'YouTube Preferred Languages',
      allow_local_processing: 'Allow Local Processing',
      deepseek_ds_session_id: 'DeepSeek Session ID',
      deepseek_authorization_token: 'DeepSeek Authorization Token',
      deepseek_pc_url: 'DeepSeek PC URL',
    }
    return labels[key] || key
  }

  return (
    <div class="max-w-3xl space-y-6">
      <div class="flex items-center justify-between">
        <h1 class="text-2xl font-bold">{t('navigation.settings')}</h1>
        <div class="flex gap-2">
          <button
            class="btn-ghost text-xs px-2 py-1"
            onClick={() => void refreshSettings()}
          >
            {t('common.refresh')}
          </button>
          <button
            classList={{
              'btn text-xs px-2 py-1': true,
              'bg-accent text-white': editMode(),
            }}
            onClick={toggleEditMode}
            disabled={!settings() || saving()}
          >
            {editMode() ? t('common.cancel') : t('common.edit')}
          </button>
        </div>
      </div>

      {/* Language */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">{t('common.language')}</h2>
        <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">
          <For each={LANGUAGES}>
            {(lang) => (
              <button
                onClick={() => void handleLanguage(lang.code)}
                classList={{
                  'px-3 py-2 rounded text-sm border transition-colors text-left': true,
                  'bg-accent/10 border-accent text-accent': language() === lang.code,
                  'border-border hover:bg-muted': language() !== lang.code,
                }}
              >
                {lang.label}
              </button>
            )}
          </For>
        </div>
      </section>

      {/* Local Processing */}
      <section class="card p-4">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-base font-semibold">{t('common.allow_local_processing')}</h2>
          <span classList={{
            'badge': true,
            'bg-teal-tint text-teal': settings()?.allow_local_processing,
            'bg-muted text-muted-foreground': !settings()?.allow_local_processing,
          }}>
            {settings()?.allow_local_processing ? t('common.done') : t('common.error')}
          </span>
        </div>
        <p class="text-xs text-muted-foreground mb-3">
          Disabled by default to avoid straining low-end devices.
        </p>
        <Show when={editMode()}>
          <div class="flex items-center gap-2">
            <input
              type="checkbox"
              checked={editValues().allow_local_processing === true}
              onChange={(e) =>
                handleEditValue('allow_local_processing', e.currentTarget.checked)
              }
            />
            <label class="text-sm">{t('common.allow_local_processing')}</label>
          </div>
        </Show>
      </section>

      {/* DeepSeek Web */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">DeepSeek Web</h2>
        <Show when={deepseek()} fallback={<p class="text-xs text-muted-foreground">{t('common.loading')}</p>}>
          <div class="space-y-3 text-sm">
            <div class="flex items-center justify-between">
              <span>Credentials Configured</span>
              <span classList={{
                'badge': true,
                'bg-teal-tint text-teal': deepseek()!.credentials_configured,
                'bg-muted text-muted-foreground': !deepseek()!.credentials_configured,
              }}>
                {deepseek()!.credentials_configured ? t('common.done') : t('common.error')}
              </span>
            </div>
            <div class="flex items-center justify-between">
              <span>Has Session ID</span>
              <span classList={{
                'badge': true,
                'bg-teal-tint text-teal': deepseek()!.has_session_id,
                'bg-muted text-muted-foreground': !deepseek()!.has_session_id,
              }}>
                {deepseek()!.has_session_id ? t('common.done') : t('common.error')}
              </span>
            </div>
            <div class="flex items-center justify-between">
              <span>Has Authorization Token</span>
              <span classList={{
                'badge': true,
                'bg-teal-tint text-teal': deepseek()!.has_authorization_token,
                'bg-muted text-muted-foreground': !deepseek()!.has_authorization_token,
              }}>
                {deepseek()!.has_authorization_token ? t('common.done') : t('common.error')}
              </span>
            </div>
          </div>
        </Show>
        <div class="flex gap-2 mt-3">
          <button
            class="btn-ghost text-xs px-2 py-1"
            onClick={() => void deepseekApi.refresh()}
            disabled={saving()}
          >
            {t('common.refresh')}
          </button>
          <button
            class="btn text-xs px-2 py-1"
            onClick={() => void handleDeepSeekConfigure()}
            disabled={saving()}
          >
            {t('common.configure')}
          </button>
          <button
            class="btn-ghost text-xs px-2 py-1"
            onClick={() => {
              window.open('https://chat.deepseek.com', '_blank')
            }}
            disabled={saving()}
            title="Open DeepSeek chat in a new tab to log in"
          >
            Open Login
          </button>
          <button
            class="btn-ghost text-xs px-2 py-1"
            onClick={() => void verifyDeepSeek()}
            disabled={saving()}
          >
            {deepseekAvailable() === true ? 'Online' : deepseekAvailable() === false ? 'Offline' : 'Check'}
          </button>
        </div>
        <Show when={editMode()}>
          <div class="space-y-2 mt-4">
            <div>
              <label class="label block text-sm mb-1">DeepSeek Session ID</label>
              <input
                class="input text-sm"
                value={editValues().deepseek_ds_session_id ?? ''}
                onInput={(e) =>
                  handleEditValue('deepseek_ds_session_id', e.currentTarget.value)
                }
                placeholder="ds_session_id cookie"
              />
            </div>
            <div>
              <label class="label block text-sm mb-1">Authorization Token</label>
              <input
                class="input text-sm"
                type="password"
                value={editValues().deepseek_authorization_token ?? ''}
                onInput={(e) =>
                  handleEditValue(
                    'deepseek_authorization_token',
                    e.currentTarget.value,
                  )
                }
                placeholder="authorization token"
              />
            </div>
            <div>
              <label class="label block text-sm mb-1">DeepSeek PC URL</label>
              <input
                class="input text-sm"
                value={editValues().deepseek_pc_url ?? ''}
                onInput={(e) =>
                  handleEditValue('deepseek_pc_url', e.currentTarget.value)
                }
                placeholder="https://chat.deepseek.com"
              />
            </div>
          </div>
        </Show>
      </section>

      {/* Browser AI */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">Browser AI</h2>
        <Show when={browserAi()} fallback={<p class="text-xs text-muted-foreground">{t('common.loading')}</p>}>
          <div class="space-y-3 text-sm">
            <div class="flex items-center justify-between">
              <span>Google AI Mode</span>
              <span classList={{
                'badge': true,
                'bg-teal-tint text-teal': browserAi()!.google_ai_mode.configured,
                'bg-muted text-muted-foreground': !browserAi()!.google_ai_mode.configured,
              }}>
                {browserAi()!.google_ai_mode.configured ? t('common.done') : t('common.error')}
              </span>
            </div>
            <div class="flex items-center justify-between">
              <span>Duck.ai</span>
              <span classList={{
                'badge': true,
                'bg-teal-tint text-teal': browserAi()!.duck_ai.configured,
                'bg-muted text-muted-foreground': !browserAi()!.duck_ai.configured,
              }}>
                {browserAi()!.duck_ai.configured ? t('common.done') : t('common.error')}
              </span>
            </div>
            <div class="text-xs text-muted-foreground">
              Rate limits: Duck.ai {browserAi()!.rate_limits.duck_ai} · Google {browserAi()!.rate_limits.google_ai_mode}
            </div>
            <div class="flex gap-2 mt-3">
              <button
                class="btn-ghost text-xs px-2 py-1"
                onClick={() => void browserAiApi.status()}
                disabled={saving()}
              >
                {t('common.refresh')}
              </button>
            </div>
          </div>
        </Show>
        <Show when={editMode()}>
          <div class="space-y-2 mt-4">
            <div>
              <label class="label block text-sm mb-1">Google AI Mode URL</label>
              <input
                class="input text-sm"
                value={editValues().default_content_processing_engine_url ?? ''}
                onInput={(e) =>
                  handleEditValue(
                    'default_content_processing_engine_url',
                    e.currentTarget.value,
                  )
                }
                placeholder="https://google.com/search"
              />
            </div>
          </div>
        </Show>
      </section>

      {/* Content Processing */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">Content Processing</h2>
        <Show when={editMode()}>
          <div class="space-y-3">
            <div>
              <label class="label block text-sm mb-1">{fieldLabel('default_content_processing_engine_doc')}</label>
              <select
                class="input text-sm"
                value={editValues().default_content_processing_engine_doc ?? 'auto'}
                onChange={(e) =>
                  handleEditValue(
                    'default_content_processing_engine_doc',
                    e.currentTarget.value,
                  )
                }
              >
                <option value="auto">Auto</option>
                <option value="docling">Docling</option>
                <option value="simple">Simple</option>
              </select>
            </div>
            <div>
              <label class="label block text-sm mb-1">{fieldLabel('default_content_processing_engine_url')}</label>
              <select
                class="input text-sm"
                value={editValues().default_content_processing_engine_url ?? 'auto'}
                onChange={(e) =>
                  handleEditValue(
                    'default_content_processing_engine_url',
                    e.currentTarget.value,
                  )
                }
              >
                <option value="auto">Auto</option>
                <option value="firecrawl">Firecrawl</option>
                <option value="jina">Jina</option>
                <option value="crawl4ai">Crawl4AI</option>
                <option value="simple">Simple</option>
              </select>
            </div>
            <div>
              <label class="label block text-sm mb-1">{fieldLabel('default_embedding_option')}</label>
              <select
                class="input text-sm"
                value={editValues().default_embedding_option ?? 'ask'}
                onChange={(e) =>
                  handleEditValue('default_embedding_option', e.currentTarget.value)
                }
              >
                <option value="ask">Ask</option>
                <option value="always">Always</option>
                <option value="never">Never</option>
              </select>
            </div>
            <div>
              <label class="label block text-sm mb-1">{fieldLabel('auto_delete_files')}</label>
              <select
                class="input text-sm"
                value={editValues().auto_delete_files ?? 'no'}
                onChange={(e) =>
                  handleEditValue('auto_delete_files', e.currentTarget.value)
                }
              >
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
            </div>
          </div>
        </Show>
      </section>

      {/* Document Processing */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">Document Processing</h2>
        <Show when={editMode()}>
          <div class="space-y-3">
            <div class="flex items-center gap-2">
              <input
                type="checkbox"
                checked={editValues().docling_ocr === true}
                onChange={(e) =>
                  handleEditValue('docling_ocr', e.currentTarget.checked)
                }
              />
              <label class="text-sm">{fieldLabel('docling_ocr')}</label>
            </div>
            <div class="flex items-center gap-2">
              <input
                type="checkbox"
                checked={editValues().docling_formulas === true}
                onChange={(e) =>
                  handleEditValue('docling_formulas', e.currentTarget.checked)
                }
              />
              <label class="text-sm">{fieldLabel('docling_formulas')}</label>
            </div>
            <div class="flex items-center gap-2">
              <input
                type="checkbox"
                checked={editValues().docling_vision === true}
                onChange={(e) =>
                  handleEditValue('docling_vision', e.currentTarget.checked)
                }
              />
              <label class="text-sm">{fieldLabel('docling_vision')}</label>
            </div>
          </div>
        </Show>
      </section>

      {/* Save controls */}
      <Show when={editMode() && Object.keys(editValues()).length > 0}>
        <div class="flex gap-3 pt-4">
          <button
            class="btn"
            onClick={() => void handleSave()}
            disabled={saving()}
          >
            {saving() ? t('common.processing') : t('common.save')}
          </button>
          <button
            class="btn-ghost text-xs px-2 py-1"
            onClick={() => {
              setEditValues({})
              setEditMode(false)
            }}
          >
            {t('common.cancel')}
          </button>
        </div>
      </Show>
      <Show when={saved()}>
        <span class="text-xs text-teal">{t('common.success')}</span>
      </Show>
    </div>
  )
}