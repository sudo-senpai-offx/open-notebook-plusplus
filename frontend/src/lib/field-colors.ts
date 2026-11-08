/**
 * Field color mapping for Study OS course classification.
 * Each field gets a unique color for visual categorization.
 */
export interface FieldColor {
  badge: string
  badgeSoft: string
  text: string
}

export const FIELD_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'art', label: 'Art' },
  { value: 'music', label: 'Music' },
  { value: 'biology', label: 'Biology' },
  { value: 'astronomy', label: 'Astronomy' },
  { value: 'geography', label: 'Geography' },
  { value: 'general', label: 'General' },
  { value: 'cultural', label: 'Cultural' },
  { value: 'custom', label: 'Custom' },
]

export const FIELD_COLORS: Record<string, FieldColor> = {
  art: {
    badge: "bg-field-art-soft text-field-art border-field-art/30",
    badgeSoft: "bg-field-art-soft",
    text: "text-field-art",
  },
  music: {
    badge: "bg-field-music-soft text-field-music border-field-music/30",
    badgeSoft: "bg-field-music-soft",
    text: "text-field-music",
  },
  biology: {
    badge: "bg-field-biology-soft text-field-biology border-field-biology/30",
    badgeSoft: "bg-field-biology-soft",
    text: "text-field-biology",
  },
  astronomy: {
    badge: "bg-field-astronomy-soft text-field-astronomy border-field-astronomy/30",
    badgeSoft: "bg-field-astronomy-soft",
    text: "text-field-astronomy",
  },
  geography: {
    badge: "bg-field-geography-soft text-field-geography border-field-geography/30",
    badgeSoft: "bg-field-geography-soft",
    text: "text-field-geography",
  },
  general: {
    badge: "bg-field-general-soft text-field-general border-field-general/30",
    badgeSoft: "bg-field-general-soft",
    text: "text-field-general",
  },
  cultural: {
    badge: "bg-field-cultural-soft text-field-cultural border-field-cultural/30",
    badgeSoft: "bg-field-cultural-soft",
    text: "text-field-cultural",
  },
  custom: {
    badge: "bg-field-unused-soft text-field-unused border-field-unused/30",
    badgeSoft: "bg-field-unused-soft",
    text: "text-field-unused",
  },
  unused: {
    badge: "bg-field-unused-soft text-field-unused border-field-unused/30",
    badgeSoft: "bg-field-unused-soft",
    text: "text-field-unused",
  },
}

export const FIELD_LABELS: Record<string, string> = {
  art: "Art",
  music: "Music",
  biology: "Biology",
  astronomy: "Astronomy",
  geography: "Geography",
  general: "General",
  cultural: "Cultural",
  custom: "Custom",
  unused: "Unused",
}

export function getFieldColor(field: string | null | undefined): FieldColor {
  return FIELD_COLORS[field ?? "unused"] ?? FIELD_COLORS["unused"]
}

export function getFieldLabel(field: string | null | undefined): string {
  return FIELD_LABELS[field ?? "unused"] ?? FIELD_LABELS["unused"]
}
