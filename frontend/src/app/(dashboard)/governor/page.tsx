import { headers } from "next/headers"
import { WakeButton } from "./wake-button"

// The server component always runs in the same container as the API,
// so the API is always reachable at http://localhost:5055/api regardless
// of how the end user reached this page (direct, port forward, reverse proxy).
const API_BASE = "http://localhost:5055/api"

export default async function GovernorPage() {
  const headersList = await headers()
  const authHeader = headersList.get("authorization") ?? undefined

  async function governorState() {
    const res = await fetch(`${API_BASE}/governor/state`, {
      cache: "no-store",
      headers: authHeader ? { authorization: authHeader } : {},
    })
    if (!res.ok) return { asleep: false }
    return res.json()
  }

  const state = await governorState()
  const isAsleep = state?.asleep ?? false

  return (
    <div className="flex-1 overflow-y-auto p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="font-display text-2xl font-bold tracking-tight">
            Governor & Hibernate
          </h1>
          <p className="text-muted-foreground text-sm">
            The governor idles the worker when there is no work for the
            configured idle window, and wakes it on demand.
          </p>
        </div>
      </div>

      <div className="bg-card border border-border p-6 rounded-lg">
        <h3 className="text-sm font-medium">Worker Status</h3>
        <p className="text-xs text-muted-foreground mt-2">
          {isAsleep ? "Asleep (hibernated)" : "Awake"}
        </p>
      </div>

      <div className="mt-6">
        <WakeButton isAsleep={isAsleep} apiBase={API_BASE} authHeader={authHeader} />
      </div>
    </div>
  )
}
