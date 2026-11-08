"use client"

import { useState } from "react"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { useTranslation } from "@/lib/hooks/use-translation"
import { assessmentApi } from "@/lib/api/settings"
import type { AssessmentCreate } from "@/lib/types/api"

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

const TYPE_OPTIONS = [
  { value: "quiz", label: "Quiz" },
  { value: "exam", label: "Exam" },
  { value: "mock", label: "Mock Exam" },
]

interface AssessmentInputProps {
  notebookId?: string | null
  onAssessmentCreated?: () => void
}

export function AssessmentInput({ notebookId, onAssessmentCreated }: AssessmentInputProps) {
  const { t } = useTranslation()
  const [field, setField] = useState("")
  const [type, setType] = useState("quiz")
  const [score, setScore] = useState("")
  const [totalQuestions, setTotalQuestions] = useState("")
  const [correctAnswers, setCorrectAnswers] = useState("")
  const [notes, setNotes] = useState("")
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setSuccess(null)
    setCreating(true)

    try {
      const data: AssessmentCreate = {
        notebook_id: notebookId || "",
        field,
        type: type as "quiz" | "exam" | "mock",
        score: parseInt(score, 10),
        total_questions: totalQuestions ? parseInt(totalQuestions, 10) : undefined,
        correct_answers: correctAnswers ? parseInt(correctAnswers, 10) : undefined,
        notes: notes || undefined,
      }
      await assessmentApi.create(data)
      setSuccess(t("settings.assessmentCreated"))
      setField("")
      setType("quiz")
      setScore("")
      setTotalQuestions("")
      setCorrectAnswers("")
      setNotes("")
      onAssessmentCreated?.()
    } catch {
      setError(t("settings.assessmentCreateFailed"))
    } finally {
      setCreating(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("settings.assessmentInput")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="assessment-field">{t("settings.fieldOfStudy")}</Label>
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
              <Label htmlFor="assessment-type">{t("settings.assessmentType")}</Label>
              <Select
                value={type}
                onValueChange={setType}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder={t("settings.selectType")} />
                </SelectTrigger>
                <SelectContent>
                  {TYPE_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="space-y-2">
              <Label htmlFor="assessment-score">{t("settings.score")} (0-100)</Label>
              <Input
                id="assessment-score"
                type="number"
                min="0"
                max="100"
                value={score}
                onChange={(e) => setScore(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="assessment-total">{t("settings.totalQuestions")}</Label>
              <Input
                id="assessment-total"
                type="number"
                min="1"
                value={totalQuestions}
                onChange={(e) => setTotalQuestions(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="assessment-correct">{t("settings.correctAnswers")}</Label>
              <Input
                id="assessment-correct"
                type="number"
                min="0"
                value={correctAnswers}
                onChange={(e) => setCorrectAnswers(e.target.value)}
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="assessment-notes">{t("settings.postCompletionReflection")}</Label>
            <Textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              placeholder={t("settings.reflectionPlaceholder")}
            />
          </div>
          {error && (
            <div className="rounded-md bg-red-50 p-3 text-sm text-red-600">{error}</div>
          )}
          {success && (
            <div className="rounded-md bg-green-50 p-3 text-sm text-green-600">{success}</div>
          )}
          <Button type="submit" disabled={creating || !field || !score}>
            {creating ? t("common.creating") : t("settings.recordAssessment")}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
