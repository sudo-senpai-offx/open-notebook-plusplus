# Pending

## Wake button UI
Checkpoint: Implementation
Waiting for: confirmation
Since: 2026-10-11
Proposal: ConnectionGuard draining banner gains Wake button -> POST /api/governor/wake (authed apiClient) -> success clears banner via config refetch, failure shows error; lib/api/governor.ts + vitest (mocked client); npm lint/test/build.
Scope: frontend/src/lib/api/governor.ts(+test), ConnectionGuard.tsx, en-US + 13-locale key mirror (wake, waking, wakeFailed)
