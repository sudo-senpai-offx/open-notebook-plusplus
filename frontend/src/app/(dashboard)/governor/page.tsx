"use client"

import { useEffect, useState } from "react"
import { AppShell } from "@/components/layout/AppShell"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { useTranslation } from "@/lib/hooks/use-translation"
import { governorApi } from "@/lib/api/governor"
import type { GovernorState } from "@/lib/api/governor"

export default function GovernorPage() {
  const { t } = useTranslation()
  const [state, setState] = useState<GovernorState | null>(null)
  const [waking, setWaking] = useState(false)
  const [message, setMessage] = useState<{ kind: "info" | "success" | "error"; text: string } | null>(null)

  useEffect(() => {
    governorApi
      .state()
      .then((s) => {
        setState(s)
      })
      .catch(() => {
        // ignore
      })
  }, [])

  const handleWake = async () => {
    setWaking(true)
    setMessage(null)
    try {
      await governorApi.wake()
      setState({ asleep: false })
      setMessage({ kind: "success", text: t("settings.governorWoke") })
    } catch {
      setMessage({ kind: "error", text: t("settings.governorWakeFailed") })
    } finally {
      setWaking(false)
    }
  }

  const refresh = async () => {
    try {
      const s = await governorApi.state()
      setState(s)
    } catch {
      // ignore
    }
  }

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">
              {t("settings.governorTitle")}
            </h1>
            <p className="text-muted-foreground text-sm">
              {t("settings.governorDesc")}
            </p>
          </div>
          <Button onClick={refresh} size="sm">
            {t("common.refresh")}
          </Button>
        </div>

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

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.governorStatus")}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-sm">{t("settings.governorWorker")}</span>
                  <Badge variant={state?.asleep ? "destructive" : "secondary"}>
                    {state?.asleep ? t("settings.governorAsleep") : t("settings.governorAwake")}
                  </Badge>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>{t("settings.governorControls")}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-medium">{t("settings.governorWake")}</h3>
                    <p className="text-xs text-muted-foreground">
                      {t("settings.governorWakeDesc")}
                    </p>
                  </div>
                  <Button
                    onClick={handleWake}
                    disabled={waking || !state?.asleep}
                  >
                    {waking
                      ? t("common.waking")
                      : state?.asleep
                        ? t("settings.governorWakeButton")
                        : t("settings.governorWakeDisabled")}
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </AppShell>
  )
}
// cache bust 1794147171
