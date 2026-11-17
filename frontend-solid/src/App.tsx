import { Suspense, createEffect, Show, type JSX } from 'solid-js'
import { Router, Route, Navigate } from '@solidjs/router'
import { AppShell } from '@/components/layout/AppShell'
import { LoginPage } from '@/pages/LoginPage'
import { NotebooksPage } from '@/pages/NotebooksPage'
import { SourcesPage } from '@/pages/SourcesPage'
import { AttachmentsPage } from '@/pages/AttachmentsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { authApi } from '@/lib/api/settings'

function Protected(props: { children?: JSX.Element }) {
  const authed = () => authApi.isAuthenticated()

  createEffect(() => {
    // 401 responses redirect via the axios interceptor; this also handles
    // a stale token on first load.
    if (!authed()) {
      window.location.href = '/login'
    }
  })

  return <Show when={authed()} fallback={<Navigate href="/login" />}>{props.children}</Show>
}

function App() {
  return (
    <Router>
      <Suspense fallback={<div class="p-4">Loading...</div>}>
        <Route path="/login" component={LoginPage} />
        <Route
          path="/"
          component={() => (
            <Protected>
              <AppShell>
                <NotebooksPage />
              </AppShell>
            </Protected>
          )}
        />
        <Route
          path="/notebooks"
          component={() => (
            <Protected>
              <AppShell>
                <NotebooksPage />
              </AppShell>
            </Protected>
          )}
        />
        <Route
          path="/sources"
          component={() => (
            <Protected>
              <AppShell>
                <SourcesPage />
              </AppShell>
            </Protected>
          )}
        />
        <Route
          path="/attachments"
          component={() => (
            <Protected>
              <AppShell>
                <AttachmentsPage />
              </AppShell>
            </Protected>
          )}
        />
        <Route
          path="/settings"
          component={() => (
            <Protected>
              <AppShell>
                <SettingsPage />
              </AppShell>
            </Protected>
          )}
        />
      </Suspense>
    </Router>
  )
}

export default App