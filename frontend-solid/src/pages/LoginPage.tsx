import { createSignal, Show } from 'solid-js'
import { authApi, notebookApi } from '@/lib/api/settings'

export function LoginPage() {
  const [password, setPassword] = createSignal('')
  const [error, setError] = createSignal('')
  const [loading, setLoading] = createSignal(false)

  const handleSubmit = async (e: SubmitEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      authApi.login(password())
      // /auth/status is unauthenticated, so verify the password against a
      // protected endpoint. A 401 here means the password is wrong.
      await notebookApi.list()
      window.location.href = '/'
    } catch (err: any) {
      if (err.response?.status === 401) {
        setError('Incorrect password')
      } else {
        setError('Unable to connect to server. Check that the API is running.')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div class="flex items-center justify-center min-h-screen px-4">
      <div class="w-full max-w-sm space-y-4">
        <div class="flex items-center justify-center gap-2 mb-6">
          <div class="w-10 h-10 rounded bg-accent flex items-center justify-center text-white font-bold">
            ON
          </div>
          <span class="font-bold text-xl">ON++</span>
        </div>
        <form onSubmit={handleSubmit} class="space-y-3">
          <input
            type="password"
            placeholder="Password"
            value={password()}
            onInput={(e) => setPassword(e.currentTarget.value)}
            class="w-full rounded border border-border bg-bg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
            required
          />
          <Show when={error()}>
            <div class="text-xs text-destructive">{error()}</div>
          </Show>
          <button
            type="submit"
            disabled={loading()}
            class="w-full btn disabled:opacity-50"
          >
            {loading() ? '...' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}