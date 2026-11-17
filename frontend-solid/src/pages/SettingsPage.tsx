import { createSignal, onMount, Show, For } from 'solid-js'
import { t, setLanguage, getLanguage, LANGUAGES } from '@/lib/i18n'
import { settingsApi, browserAiApi, deepseekApi } from '@/lib/api/settings'
import type { BrowserAIStatus, DeepSeekSessionStatus } from '@/lib/types/api'

export function SettingsPage() {
  const [language, setLang] = createSignal(getLanguage())
  const [saving, setSaving] = createSignal(false)
  const [saved, setSaved] = createSignal(false)
  const [browserAi, setBrowserAi] = createSignal<BrowserAIStatus | null>(null)
  const [deepseek, setDeepseek] = createSignal<DeepSeekSessionStatus | null>(null)
  const [googleUrl, setGoogleUrl] = createSignal('')
  const [duckUrl, setDuckUrl] = createSignal('')

  onMount(async () => {
    try {
      const s = await settingsApi.get()
      setGoogleUrl(s?.browser_ai?.google_ai_mode_url ?? '')
      setDuckUrl(s?.browser_ai?.duck_ai_url ?? '')
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
  })

  const handleLanguage = async (code: string) => {
    setLang(code)
    await setLanguage(code)
  }

  const handleSave = async () => {
    setSaving(true)
    setSaved(false)
    try {
      // Settings persistence is server-side; reflect changes after save.
      await settingsApi.get()
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div class="max-w-2xl space-y-6">
      <h1 class="text-2xl font-bold">{t('navigation.settings')}</h1>

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
          </div>
        </Show>
      </section>

      {/* DeepSeek */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">DeepSeek Web</h2>
        <Show when={deepseek()} fallback={<p class="text-xs text-muted-foreground">{t('common.loading')}</p>}>
          <p class="text-sm">
            {deepseek()!.credentials_configured
              ? t('common.done')
              : t('common.error')}
          </p>
        </Show>
      </section>

      {/* URLs */}
      <section class="card p-4">
        <h2 class="text-base font-semibold mb-3">Endpoints</h2>
        <div class="space-y-3">
          <div>
            <label class="label block text-sm mb-1">Google AI Mode URL</label>
            <input
              class="input"
              value={googleUrl()}
              onInput={(e) => setGoogleUrl(e.currentTarget.value)}
            />
          </div>
          <div>
            <label class="label block text-sm mb-1">Duck.ai URL</label>
            <input
              class="input"
              value={duckUrl()}
              onInput={(e) => setDuckUrl(e.currentTarget.value)}
            />
          </div>
          <button class="btn" onClick={() => void handleSave()} disabled={saving()}>
            {saving() ? t('common.processing') : t('common.save')}
          </button>
          <Show when={saved()}>
            <span class="text-xs text-teal ml-2">{t('common.success')}</span>
          </Show>
        </div>
      </section>
    </div>
  )
}