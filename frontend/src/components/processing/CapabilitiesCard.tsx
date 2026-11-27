'use client'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { useTranslation } from '@/lib/hooks/use-translation'
import { processingApi, type HealthReadyCapability } from '@/lib/api/processing'

/** Capability name -> human-readable label + install hint for missing tools. */
const CAPABILITY_INFO: Record<
  keyof HealthReadyCapability,
  { label: string; hint: string }
> = {
  pdftotext: {
    label: 'pdftotext',
    hint: 'Install poppler for PDF text extraction',
  },
  pdftoppm: {
    label: 'pdftoppm',
    hint: 'Install poppler for PDF page rendering (OCR)',
  },
  tesseract: {
    label: 'tesseract',
    hint: 'Install tesseract for scanned-document OCR',
  },
  pandoc: {
    label: 'pandoc',
    hint: 'Install pandoc for document conversion (docx/odt/epub/rtf)',
  },
  ssconvert: {
    label: 'ssconvert',
    hint: 'Install Gnumeric for spreadsheet conversion',
  },
  whisper_cli: {
    label: 'whisper-cli',
    hint: 'Install whisper.cpp for audio transcription',
  },
  ffmpeg: {
    label: 'ffmpeg',
    hint: 'Install ffmpeg for audio normalization',
  },
  content_core: {
    label: 'content-core',
    hint: 'Python package for URL/text extraction fallback',
  },
}

/** Displays installed vs. missing status badges for runtime extraction
 *  capabilities, probed via GET /health/ready. Missing tools carry a brief
 *  install hint so users understand why OCR/STT/DOCX gates behave as they do.
 *
 *  All new strings are English-only for now and tagged // i18n-todo so the
 *  post-stabilization translation pass can find them.
 */
export function CapabilitiesCard() {
  const { t } = useTranslation()
  const [data, setData] = useState<HealthReadyCapability | null>(null)
  const [error, setError] = useState<boolean>(false)

  const fetchCapabilities = useCallback(async () => {
    try {
      const response = await processingApi.getHealthReady()
      setData(response.capabilities)
      setError(false)
    } catch {
      setError(true)
    }
  }, [])

  useEffect(() => {
    fetchCapabilities()
  }, [fetchCapabilities])

  const entries = Object.entries(CAPABILITY_INFO) as Array<[
    keyof HealthReadyCapability,
    { label: string; hint: string },
  ]>

  // i18n-todo: new locale keys needed for capability card labels/hints
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {/* i18n-todo */}
          <span className="font-mono text-xs text-muted-foreground">⚙</span>
          {t('capabilities.title')}
        </CardTitle>
        <CardDescription>{t('capabilities.desc')}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {error ? (
          <div className="text-xs text-muted-foreground">
            {t('capabilities.unavailable')}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 text-xs">
            {entries.map(([key, info]) => {
              const available = data?.[key] ?? false
              // i18n-todo: capability labels and install hints
              return (
                <Badge
                  key={key}
                  variant={available ? 'default' : 'secondary'}
                  title={available
                    ? info.label
                    : `${info.label} — ${info.hint}`}
                >
                  {info.label}
                </Badge>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

import { useState, useEffect, useCallback } from 'react'
