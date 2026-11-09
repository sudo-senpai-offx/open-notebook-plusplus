"use client"

import { useEffect, useState } from "react"

const API_BASE = "/api"

async function governorWake() {
  const res = await fetch(`${API_BASE}/governor/wake`, { method: "POST" })
  if (!res.ok) throw new Error("wake failed")
  return res.json()
}

interface WakeButtonProps {
  isAsleep: boolean
}

export function WakeButton({ isAsleep }: WakeButtonProps) {
  const [waking, setWaking] = useState(false)
  const [message, setMessage] = useState<{ kind: "info" | "success" | "error"; text: string } | null>(null)

  const handleWake = async () => {
    setWaking(true)
    setMessage(null)
    try {
      await governorWake()
      setMessage({ kind: "success", text: "Worker woke successfully" })
    } catch {
      setMessage({ kind: "error", text: "Failed to wake worker" })
    } finally {
      setWaking(false)
    }
  }

  return (
    <div className="bg-card border border-border p-6 rounded-lg">
      <h3 className="text-sm font-medium">Wake Worker</h3>
      <p className="text-xs text-muted-foreground mt-2">
        Restart the worker after governor hibernation. Only available when
        asleep.
      </p>
      <button
        onClick={handleWake}
        disabled={waking || !isAsleep}
        className="mt-4 px-4 py-2 bg-primary text-primary-font rounded hover:bg-primary/90 disabled:opacity-50"
      >
        {waking ? "Waking…" : isAsleep ? "Wake" : "Not asleep"}
      </button>
      {message && (
        <div
          className={`rounded-md p-3 text-sm ${
            message.kind === "success"
              ? "bg-green-50 text-green-700"
              : message.kind === "error"
              ? "bg-red-50 text-red-700"
              : "bg-blue-50 text-blue-700"
          }`}
        >
          {message.text}
        </div>
      )}
    </div>
  )
}
