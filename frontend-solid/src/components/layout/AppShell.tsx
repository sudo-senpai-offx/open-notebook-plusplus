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
      {/* Mobile backdrop */}
      <Show when={sidebarOpen()}>
        <div
          class="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
          onClick={() => setSidebarOpen(false)}
        />
      </Show>

      {/* Sidebar */}
      <aside
        classList={{
          'fixed inset-y-0 left-0 z-50 w-[260px] bg-card border-r border-border transition-transform duration-300': true,
          'translate-x-0': sidebarOpen(),
          '-translate-x-full': !sidebarOpen(),
        }}
      >
        <div class="flex flex-col h-full">
          <div class="p-5 border-b border-border">
            <div class="flex items-center gap-2.5">
              <div class="w-8 h-8 rounded-md bg-accent flex items-center justify-center text-white font-bold text-xs">
                ON
              </div>
              <span class="font-bold text-lg tracking-tight">ON++</span>
            </div>
          </div>

          <nav class="flex-1 overflow-y-auto p-3 space-y-0.5">
            <For each={navItems}>
              {(item) => (
                <A
                  href={item.href}
                  onClick={() => setSidebarOpen(false)}
                  classList={{
                    'sidebar-item': true,
                    'sidebar-item active': item.href === locationHref(),
                    'hover:bg-muted': true,
                  }}
                >
                  <item.icon class="w-4 h-4 flex-shrink-0" />
                  <span class="flex-1">{item.name}</span>
                </A>
              )}
            </For>
          </nav>

          <div class="p-3 border-t border-border">
            <div
              classList={{
                'sidebar-item': true,
                'hover:bg-muted': true,
              }}
              onClick={handleLogout}
            >
              <LogOut class="w-4 h-4 flex-shrink-0" />
              <span class="flex-1">{t('common.signOut')}</span>
            </div>
            <div class="mt-3">
              <label class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5 block">
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
            class="lg:hidden mr-3 p-2 rounded-md hover:bg-muted transition-colors"
            onClick={() => setSidebarOpen(!sidebarOpen())}
          >
            <Menu class="w-5 h-5" />
          </button>
          <div class="flex-1">
            <h1 class="text-lg font-bold tracking-tight">ON++</h1>
          </div>
          <button
            class="p-2 rounded-md hover:bg-muted transition-colors"
            onClick={handleLogout}
          >
            <LogOut class="w-4 h-4" />
          </button>
        </header>

        <main class="flex-1 overflow-y-auto p-4 lg:p-6">
          {props.children}
        </main>
      </div>
    </div>
  )
}

function locationHref(): string {
  if (typeof window !== 'undefined') {
    return window.location.pathname
  }
  return '/'
}