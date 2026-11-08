"use client"

import { useEffect, useState } from "react"
import { AppShell } from "@/components/layout/AppShell"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { useTranslation } from "@/lib/hooks/use-translation"
import { assessmentApi } from "@/lib/api/settings"
import type { SpiderwebMetric } from "@/lib/types/api"
import { MasteryRadar } from "@/components/ui/mastery-radar"
import { AssessmentInput } from "@/components/assessments/AssessmentInput"

export default function AssessmentsPage() {
  const { t } = useTranslation()
  const [metrics, setMetrics] = useState<SpiderwebMetric[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    assessmentApi.spiderweb().then((data) => {
      setMetrics(data.fields)
      setLoading(false)
    }).catch(() => {
      setLoading(false)
    })
  }, [])

  const axes = metrics.map((m) => ({
    label: m.field.charAt(0).toUpperCase() + m.field.slice(1),
    value: m.score,
  }))

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">
              {t("settings.assessmentTitle")}
            </h1>
            <p className="text-muted-foreground text-sm">
              {t("settings.assessmentDesc")}
            </p>
          </div>
          <Button
            onClick={() => assessmentApi.spiderweb().then((d) => setMetrics(d.fields))}
            size="sm"
          >
            {t("common.refresh")}
          </Button>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Spiderweb Chart */}
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>{t("settings.spiderwebChart")}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col items-center justify-center min-h-[400px]">
              {loading ? (
                <div className="animate-pulse">
                  <div className="w-16 h-16 rounded-full border-4 border-muted/20" />
                </div>
              ) : axes.length === 0 ? (
                <div className="text-center text-muted-foreground">
                  <p>{t("settings.noAssessmentData")}</p>
                </div>
              ) : (
                <MasteryRadar
                  axes={axes}
                  size={320}
                  rings={4}
                  label={t("settings.capabilityAnalysis")}
                />
              )}
            </CardContent>
          </Card>

          {/* Metrics detail */}
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.fieldMetrics")}</CardTitle>
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="h-32 animate-pulse rounded-lg bg-muted" />
              ) : axes.length === 0 ? (
                <p className="text-muted-foreground text-sm">
                  {t("settings.noAssessmentDataDesc")}
                </p>
              ) : (
                <div className="space-y-3">
                  {axes.map((axis) => (
                    <div key={axis.label} className="flex items-center justify-between">
                      <span className="text-sm">{axis.label}</span>
                      <div className="w-24 bg-muted rounded-full h-2.5">
                        <div
                          className="bg-primary h-2.5 rounded-full transition-all duration-500"
                          style={{ width: `${axis.value}%` }}
                        />
                      </div>
                      <span className="text-xs font-mono text-muted-foreground">
                        {axis.value.toFixed(0)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="mt-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.recordAssessment")}</CardTitle>
            </CardHeader>
            <CardContent>
              <AssessmentInput
                onAssessmentCreated={() => {
                  assessmentApi.spiderweb().then((data) => setMetrics(data.fields))
                }}
              />
            </CardContent>
          </Card>
        </div>
      </div>
    </AppShell>
  )
}
