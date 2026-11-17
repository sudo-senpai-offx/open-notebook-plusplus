import { createSignal, For, Show, type JSX } from 'solid-js'
import { A, useNavigate } from '@solidjs/router'
import { t, setLanguage, getLanguage, LANGUAGES } from '@/lib/i18n'
import { authApi } from '@/lib/api/settings'
import { LogOut, FileText, Zap, Bot, Settings, Menu } from 'lucide-solid'

export function AppShell(props: { children: JSX.Element }) {
  const [sidebarOpen, setSidebarOpen] = createSignal(false)
  const [language, setLanguageSignal] = createSignal(getLanguage())
  const navigate = useNavigate()

  const handleLogout = () => {
    authApi.logout()
    navigate('/login')
  }

  const handleLanguageChange = async (lang: string) => {
    await setLanguage(lang)
    setLanguageSignal(lang)
  }

  const navItems = [
    { name: t('navigation.notebooks'), href: '/notebooks', icon: FileText },
    { name: t('navigation.sources'), href: '/sources', icon: Bot },
    { name: t('navigation.attachments'), href: '/attachments', icon: Zap },
    { name: t('navigation.settings'), href: '/settings', icon: Settings },
  ]

  return (
    <div class="flex h-screen bg-bg text-text">
      <Show when={sidebarOpen()}>
        <div
          class="fixed inset-0 z-50 bg-black/50"
          onClick={() => setSidebarOpen(false)}
        />
      </Show>

      <aside
        classList={{
          'fixed inset-y-0 left-0 z-50 w-64 bg-card border-r border-border transition-transform duration-200': true,
          'translate-x-0': sidebarOpen(),
          '-translate-x-full': !sidebarOpen(),
        }}
      >
        <div class="flex flex-col h-full">
          <div class="p-4 border-b border-border">
            <div class="flex items-center gap-2">
              <div class="w-8 h-8 rounded bg-accent flex items-center justify-center text-white font-bold text-sm">
                ON
              </div>
              <span class="font-bold text-lg">ON++</span>
            </div>
          </div>

          <nav class="flex-1 overflow-y-auto p-4 space-y-1">
            <For each={navItems}>
              {(item) => (
                <A
                  href={item.href}
                  onClick={() => setSidebarOpen(false)}
                  class="flex items-center gap-3 px-3 py-2 rounded text-sm transition-colors hover:bg-muted"
                  activeClass="bg-accent/10 text-accent"
                >
                  <item.icon class="w-4 h-4" />
                  <span>{item.name}</span>
                </A>
              )}
            </For>
          </nav>

          <div class="p-4 border-t border-border">
            <div
              class="flex items-center gap-3 px-3 py-2 rounded text-sm cursor-pointer hover:bg-muted transition-colors"
              onClick={handleLogout}
            >
              <LogOut class="w-4 h-4" />
              <span>{t('common.signOut')}</span>
            </div>
            <div class="mt-3">
              <label class="text-xs font-medium text-muted-foreground mb-1 block">
                {t('navigation.language')}
              </label>
              <select
                class="input text-sm"
                value={language()}
                onChange={(e) => void handleLanguageChange(e.currentTarget.value)}
              >
                <For each={LANGUAGES}>
                  {(lang) => (
                    <option value={lang.code}>{lang.label}</option>
                  )}
                </For>
              </select>
            </div>
          </div>
        </div>
      </aside>

      <div class="flex-1 flex flex-col overflow-hidden">
        <header class="flex items-center px-4 py-3 border-b border-border bg-bg">
          <button
            class="lg:hidden mr-3 p-1"
            onClick={() => setSidebarOpen(!sidebarOpen())}
          >
            <Menu class="w-5 h-5" />
          </button>
          <div class="flex-1">
            <h1 class="font-bold text-lg">ON++</h1>
          </div>
          <button
            class="p-2 rounded hover:bg-muted transition-colors"
            onClick={handleLogout}
          >
            <LogOut class="w-4 h-4" />
          </button>
        </header>

        <main class="flex-1 overflow-y-auto p-4">
          {props.children}
        </main>
      </div>
    </div>
  )
}