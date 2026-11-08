"use client"

import { useState } from "react"
import { AppShell } from "@/components/layout/AppShell"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { useTranslation } from "@/lib/hooks/use-translation"
import { dynamicCourseApi } from "@/lib/api/settings"
import type { DynamicCourseCreate, DynamicCourseModule, DynamicCourseResponse } from "@/lib/types/api"

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

export default function DynamicCoursePage() {
  const { t } = useTranslation()
  const [topic, setTopic] = useState("")
  const [field, setField] = useState("")
  const [durationDays, setDurationDays] = useState("")
  const [userPreferences, setUserPreferences] = useState("")
  const [creating, setCreating] = useState(false)
  const [course, setCourse] = useState<DynamicCourseResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setCourse(null)
    setCreating(true)

    try {
      const data: DynamicCourseCreate = {
        topic,
        field,
        estimated_duration_days: durationDays ? parseInt(durationDays, 10) : undefined,
        user_preferences: userPreferences || undefined,
      }
      const result = await dynamicCourseApi.create(data)
      setCourse(result)
    } catch {
      setError(t("settings.dynamicCourseCreateFailed"))
    } finally {
      setCreating(false)
    }
  }

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto p-6">
        <div className="mb-6">
          <h1 className="font-display text-2xl font-bold tracking-tight">
            {t("settings.dynamicCourseTitle")}
          </h1>
          <p className="text-muted-foreground text-sm">
            {t("settings.dynamicCourseDesc")}
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Form */}
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>{t("settings.createDynamicCourse")}</CardTitle>
              <CardDescription>
                {t("settings.createDynamicCourseDesc")}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSubmit} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="dc-topic">{t("settings.topicToLearn")}</Label>
                  <Input
                    value={topic}
                    onChange={(e) => setTopic(e.target.value)}
                    placeholder={t("settings.topicPlaceholder")}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="dc-field">{t("settings.fieldOfStudy")}</Label>
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
                  <Label htmlFor="dc-duration">{t("settings.estimatedDuration")}</Label>
                  <Input
                    id="dc-duration"
                    type="number"
                    min="1"
                    value={durationDays}
                    onChange={(e) => setDurationDays(e.target.value)}
                    placeholder={t("settings.durationPlaceholder")}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="dc-preferences">{t("settings.userPreferences")}</Label>
                  <Textarea
                    value={userPreferences}
                    onChange={(e) => setUserPreferences(e.target.value)}
                    rows={3}
                    placeholder={t("settings.preferencesPlaceholder")}
                  />
                </div>
                {error && (
                  <div className="rounded-md bg-red-50 p-3 text-sm text-red-600">
                    {error}
                  </div>
                )}
                <Button type="submit" disabled={creating || !topic || !field}>
                  {creating ? t("common.creating") : t("settings.createCourse")}
                </Button>
              </form>
            </CardContent>
          </Card>

          {/* Course Preview */}
          {course && (
            <Card className="lg:col-span-1">
              <CardHeader>
                <CardTitle>{t("settings.coursePreview")}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div>
                    <p className="text-sm font-medium">{t("settings.topic")}</p>
                    <p className="text-lg font-semibold">{course.topic}</p>
                  </div>
                  <div>
                    <p className="text-sm font-medium">{t("settings.field")}</p>
                    <Badge variant="outline">{course.field}</Badge>
                  </div>
                  <div>
                    <p className="text-sm font-medium">{t("settings.estimatedDuration")}</p>
                    <p className="text-sm">
                      {course.estimated_duration_days} {t("settings.days")}
                    </p>
                  </div>
                  <div>
                    <p className="text-sm font-medium">{t("settings.modules")}</p>
                    <div className="space-y-2">
                      {course.modules.map((m: DynamicCourseModule, i: number) => (
                        <div key={i} className="p-3 rounded-lg border bg-muted">
                          <p className="text-sm font-semibold">{m.module_title}</p>
                          <p className="text-xs text-muted-foreground">
                            {m.module_description}
                          </p>
                          <p className="text-xs text-muted-foreground mt-1">
                            {m.estimated_hours} {t("settings.hours")} •
                            {m.lessons.length} {t("settings.lessons")}
                          </p>
                          <div className="mt-2 flex gap-1">
                            {m.lessons.slice(0, 3).map((lesson, j) => (
                              <span key={j} className="text-[10px] px-2 py-1 rounded bg-border">
                                {lesson}
                              </span>
                            ))}
                            {m.lessons.length > 3 && (
                              <span className="text-[10px] text-muted-foreground">
                                +{m.lessons.length - 3}
                              </span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="text-sm font-medium">{t("settings.examMockTopics")}</p>
                    <div className="flex flex-wrap gap-1">
                      {course.exam_mock_topics.map((topic: string, i: number) => (
                        <span key={i} className="text-xs px-2 py-1 rounded bg-border">
                          {topic}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </AppShell>
  )
}
