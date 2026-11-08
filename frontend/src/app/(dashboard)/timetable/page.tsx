"use client"

import { useEffect, useState } from "react"
import { AppShell } from "@/components/layout/AppShell"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useTranslation } from "@/lib/hooks/use-translation"
import { timetableApi } from "@/lib/api/settings"
import type { TimetableBlockCreate, TimetableBlockResponse } from "@/lib/types/api"
import { Badge } from "@/components/ui/badge"
import { getFieldColor, getFieldLabel } from "@/lib/field-colors"

const ACTIVITY_TYPES = [
  { value: "study", label: "Study" },
  { value: "flashcard", label: "Flashcards" },
  { value: "assessment", label: "Assessment" },
  { value: "break", label: "Break" },
]

const FIELD_OPTIONS = [
  { value: "art", label: "Art" },
  { value: "music", label: "Music" },
  { value: "biology", label: "Biology" },
  { value: "astronomy", label: "Astronomy" },
  { value: "geography", label: "Geography" },
  { value: "general", label: "General" },
  { value: "cultural", label: "Cultural" },
  { value: "custom", label: "Custom" },
]

export default function TimetablePage() {
  const { t, language } = useTranslation()
  const [blocks, setBlocks] = useState<TimetableBlockResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedDate, setSelectedDate] = useState(new Date().toISOString().split("T")[0])
  const [title, setTitle] = useState("")
  const [activityType, setActivityType] = useState("study")
  const [field, setField] = useState("")
  const [startTime, setStartTime] = useState("")
  const [endTime, setEndTime] = useState("")
  const [isHighIntensity, setIsHighIntensity] = useState(false)
  const [creating, setCreating] = useState(false)
  const [burnout, setBurnout] = useState<{
    high_intensity_hours: number
    overloaded: boolean
    limit_hours: number
    message: string
  } | null>(null)

  const loadBlocks = async () => {
    try {
      const data = await timetableApi.list({ date: selectedDate })
      setBlocks(data)
    } catch {
      setBlocks([])
    } finally {
      setLoading(false)
    }
  }

  const checkBurnout = async () => {
    try {
      const data = await timetableApi.burnoutCheck(selectedDate)
      setBurnout(data)
    } catch {
      setBurnout(null)
    }
  }

  useEffect(() => {
    loadBlocks()
    checkBurnout()
  }, [selectedDate, loadBlocks, checkBurnout])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setCreating(true)
    try {
      const data: TimetableBlockCreate = {
        title,
        activity_type: activityType as TimetableBlockCreate["activity_type"],
        field: field || undefined,
        start_time: `${selectedDate}T${startTime}:00`,
        end_time: `${selectedDate}T${endTime}:00`,
        is_high_intensity: isHighIntensity,
      }
      await timetableApi.create(data)
      setTitle("")
      setStartTime("")
      setEndTime("")
      setIsHighIntensity(false)
      await loadBlocks()
      await checkBurnout()
    } catch {
      // error handled by toast
    } finally {
      setCreating(false)
    }
  }

  const formatTime = (iso: string) => {
    return new Date(iso).toLocaleTimeString(language, {
      hour: "2-digit",
      minute: "2-digit",
    })
  }

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto p-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="font-display text-2xl font-bold tracking-tight">
              {t("settings.timetableTitle")}
            </h1>
            <p className="text-muted-foreground text-sm">
              {t("settings.timetableDesc")}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <Input
              type="date"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="w-48"
            />
            <Button
              onClick={() => {
                const d = new Date(selectedDate)
                d.setDate(d.getDate() + 1)
                setSelectedDate(d.toISOString().split("T")[0])
              }}
              size="sm"
            >
              {t("common.nextDay")}
            </Button>
          </div>
        </div>

        {/* Burnout Guardrail */}
        {burnout && (
          <Card className={burnout.overloaded ? "border-destructive/50 bg-destructive-tint/50" : undefined}>
            <CardHeader>
              <CardTitle>
                {burnout.overloaded
                  ? t("settings.burnoutOverload")
                  : t("settings.burnoutSafe")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-muted-foreground">
                    {t("settings.highIntensityHours", {
                      hours: burnout.high_intensity_hours,
                    })}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {t("settings.limitHours", { hours: burnout.limit_hours })}
                  </p>
                </div>
                <Badge
                  variant={burnout.overloaded ? "destructive" : "secondary"}
                >
                  {burnout.message}
                </Badge>
              </div>
            </CardContent>
          </Card>
        )}

        <div className="mt-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Blocks List */}
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>
                {t("settings.scheduleFor", {
                  date: new Date(selectedDate).toLocaleDateString(language, {
                    weekday: "long",
                    month: "long",
                    day: "numeric",
                  }),
                })}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="h-48 animate-pulse rounded-lg bg-muted" />
              ) : blocks.length === 0 ? (
                <p className="text-muted-foreground text-sm text-center py-8">
                  {t("settings.noBlocksScheduled")}
                </p>
              ) : (
                <div className="space-y-3">
                  {blocks
                    .sort(
                      (a, b) =>
                        new Date(a.start_time).getTime() -
                        new Date(b.start_time).getTime()
                    )
                    .map((block) => (
                      <div
                        key={block.id}
                        className="flex items-center gap-3 p-3 rounded-lg border bg-surface"
                      >
                        <span className="w-3 h-3 rounded-full shrink-0" style={{ backgroundColor: getFieldColor(block.field).text.split(' ')[0].replace('text-', '') === 'field-art' ? 'var(--color-field-art)' : 'var(--color-field-music)' }} />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium truncate">
                            {block.title}
                          </p>
                          <p className="text-xs text-muted-foreground truncate">
                            {block.activity_type} • {block.field ? getFieldLabel(block.field) : t("settings.general")}
                          </p>
                        </div>
                        <div className="text-xs text-right text-muted-foreground shrink-0">
                          {formatTime(block.start_time)} - {formatTime(block.end_time)}
                        </div>
                        {block.is_high_intensity && (
                          <Badge variant="outline" className="text-[10px]">
                            {t("settings.highIntensity")}
                          </Badge>
                        )}
                      </div>
                    ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Add Block Form */}
          <Card>
            <CardHeader>
              <CardTitle>{t("settings.addBlock")}</CardTitle>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSubmit} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="tt-title">{t("settings.blockTitle")}</Label>
                  <Input
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder={t("settings.blockTitlePlaceholder")}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="tt-activity">{t("settings.activityType")}</Label>
                  <Select
                    value={activityType}
                    onValueChange={setActivityType}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder={t("settings.selectActivity")} />
                    </SelectTrigger>
                    <SelectContent>
                      {ACTIVITY_TYPES.map((opt) => (
                        <SelectItem key={opt.value} value={opt.value}>
                          {opt.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="tt-field">{t("settings.fieldOfStudy")}</Label>
                    <Select
                      value={field}
                      onValueChange={setField}
                    >
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder={t("settings.fieldPlaceholder")} />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="">{t("settings.fieldNone")}</SelectItem>
                        {FIELD_OPTIONS.map((opt) => (
                          <SelectItem key={opt.value} value={opt.value}>
                            {opt.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="tt-high-intensity">{t("settings.highIntensityBlock")}</Label>
                    <div className="flex items-center gap-2">
                      <input
                        id="tt-high-intensity"
                        type="checkbox"
                        checked={isHighIntensity}
                        onChange={(e) => setIsHighIntensity(e.target.checked)}
                        className="h-4 w-4 shrink-0 rounded border-input bg-background text-primary"
                      />
                      <Label htmlFor="tt-high-intensity" className="text-sm">
                        {t("settings.highIntensityBlockLabel")}
                      </Label>
                    </div>
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="tt-start">{t("settings.startTime")}</Label>
                    <Input
                      id="tt-start"
                      type="time"
                      value={startTime}
                      onChange={(e) => setStartTime(e.target.value)}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="tt-end">{t("settings.endTime")}</Label>
                    <Input
                      id="tt-end"
                      type="time"
                      value={endTime}
                      onChange={(e) => setEndTime(e.target.value)}
                    />
                  </div>
                </div>
                <Button type="submit" disabled={creating || !title || !startTime || !endTime}>
                  {creating ? t("common.creating") : t("settings.addBlock")}
                </Button>
              </form>
            </CardContent>
          </Card>
        </div>
      </div>
    </AppShell>
  )
}
