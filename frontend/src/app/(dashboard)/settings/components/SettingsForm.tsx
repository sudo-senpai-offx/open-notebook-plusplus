'use client'

import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { LoadingSpinner } from '@/components/common/LoadingSpinner'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Alert, AlertTitle, AlertDescription } from '@/components/ui/alert'
import { useSettings, useUpdateSettings } from '@/lib/hooks/use-settings'
import { useCapabilities } from '@/lib/hooks/use-capabilities'
import { sessionApi, quotaApi, deepseekApi } from '@/lib/api/settings'
import { DeepSeekSessionStatus } from '@/lib/types/api'
import { useEffect, useState } from 'react'
import { ChevronDownIcon } from 'lucide-react'
import { useTranslation } from '@/lib/hooks/use-translation'

const settingsSchema = z.object({
  default_content_processing_engine_doc: z.enum(['auto', 'docling', 'simple']).optional(),
  default_content_processing_engine_url: z.enum(['auto', 'firecrawl', 'jina', 'crawl4ai', 'simple']).optional(),
  default_embedding_option: z.enum(['ask', 'always', 'never']).optional(),
  auto_delete_files: z.enum(['yes', 'no']).optional(),
  docling_ocr: z.boolean().optional(),
  docling_formulas: z.boolean().optional(),
  docling_vision: z.boolean().optional(),
  allow_local_processing: z.boolean().optional(),
})

type SettingsFormData = z.infer<typeof settingsSchema>

export function SettingsForm() {
  const { t } = useTranslation()
  const { data: settings, isLoading, error } = useSettings()
  const { data: capabilities, isError: capabilitiesError } = useCapabilities()
  const updateSettings = useUpdateSettings()
  // Opt-in heavy runtimes are installed on demand at container startup, so an
  // engine is only offered when the backend probe confirms it's actually
  // available. While the probe is still loading, default to available to avoid a
  // flash of disabled controls on a correctly-configured install; but if the
  // probe *fails*, fail closed (treat as unavailable) rather than advertising an
  // engine the backend couldn't verify.
  const doclingAvailable = capabilities?.docling_available ?? !capabilitiesError
  const crawl4aiAvailable = capabilities?.crawl4ai_available ?? !capabilitiesError
  const [sessionData, setSessionData] = useState<{
    session_id?: string
    local_processing_enabled?: boolean
    cloud_processing_enabled?: boolean
    browser_session_active?: boolean
    last_refresh_at?: string
    rate_limits?: Record<string, unknown>
    allow_local_processing?: boolean
  }>({})
  const [sessionLoading, setSessionLoading] = useState(false)
  const [quotaData, setQuotaData] = useState<{
    providers: Record<string, { used: number; limit: number; remaining: number; exceeded: boolean; reset_at?: string; last_used_at?: string }>
    cumulative_used: number
    cumulative_limit: number
    cumulative_remaining: number
    cumulative_exceeded: boolean
    updated_at?: string
  }>({
    providers: {},
    cumulative_used: 0,
    cumulative_limit: 0,
    cumulative_remaining: 0,
    cumulative_exceeded: false,
  })
  const [quotaLoading, setQuotaLoading] = useState(false)
  const [deepseekStatus, setDeepseekStatus] = useState<DeepSeekSessionStatus | null>(null)
  const [deepseekLoading, setDeepseekLoading] = useState(false)
  const [deepseekConfigured, setDeepseekConfigured] = useState(false)
  const [dsSessionId, setDsSessionId] = useState('')
  const [authToken, setAuthToken] = useState('')
  const [configureMessage, setConfigureMessage] = useState<string | null>(null)
  const [configureError, setConfigureError] = useState<string | null>(null)

  useEffect(() => {
    let mounted = true
    const fetchSession = async () => {
      setSessionLoading(true)
      try {
        const data = await sessionApi.getStatus()
        if (mounted) setSessionData(data)
      } catch {
        if (mounted) setSessionData({})
      } finally {
        if (mounted) setSessionLoading(false)
      }
    }
    const fetchQuota = async () => {
      setQuotaLoading(true)
      try {
        const data = await quotaApi.getStatus()
        if (mounted) setQuotaData(data)
      } catch {
        if (mounted) setQuotaData({
          providers: {},
          cumulative_used: 0,
          cumulative_limit: 0,
          cumulative_remaining: 0,
          cumulative_exceeded: false,
        })
      } finally {
        if (mounted) setQuotaLoading(false)
      }
    }
    fetchSession()
    fetchQuota()
    const fetchDeepseek = async () => {
      setDeepseekLoading(true)
      try {
        const data = await deepseekApi.getStatus()
        if (mounted) {
          setDeepseekStatus(data)
          setDeepseekConfigured(data.credentials_configured)
        }
      } catch {
        if (mounted) setDeepseekStatus(null)
      } finally {
        if (mounted) setDeepseekLoading(false)
      }
    }
    fetchDeepseek()
    return () => { mounted = false }
  }, [])

const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({
  doc: false,
  url: false,
  embedding: false,
  files: false
})
const [showRestartModal, setShowRestartModal] = useState(false)
const [hasResetForm, setHasResetForm] = useState(false)

  const {
    control,
    handleSubmit,
    reset,
    setValue,
    watch,
    formState: { isDirty }
  } = useForm<SettingsFormData>({
    resolver: zodResolver(settingsSchema),
    defaultValues: {
      default_content_processing_engine_doc: undefined,
      default_content_processing_engine_url: undefined,
      default_embedding_option: undefined,
      auto_delete_files: undefined,
      docling_ocr: undefined,
      docling_formulas: undefined,
      docling_vision: undefined,
      allow_local_processing: undefined,
    }
  })

  const allowLocalProcessing = watch('allow_local_processing')


  const toggleSection = (section: string) => {
    setExpandedSections(prev => ({ ...prev, [section]: !prev[section] }))
  }

  useEffect(() => {
    if (settings && settings.default_content_processing_engine_doc && !hasResetForm) {
      const formData = {
        default_content_processing_engine_doc: settings.default_content_processing_engine_doc as 'auto' | 'docling' | 'simple',
        default_content_processing_engine_url: settings.default_content_processing_engine_url as 'auto' | 'firecrawl' | 'jina' | 'crawl4ai' | 'simple',
        default_embedding_option: settings.default_embedding_option as 'ask' | 'always' | 'never',
        auto_delete_files: settings.auto_delete_files as 'yes' | 'no',
        docling_ocr: settings.docling_ocr ?? true,
        docling_formulas: settings.docling_formulas ?? false,
        docling_vision: settings.docling_vision ?? false,
        allow_local_processing: settings.allow_local_processing,
      }
      reset(formData)
      setHasResetForm(true)
    }
  }, [hasResetForm, reset, settings])

  const onSubmit = async (data: SettingsFormData) => {
    await updateSettings.mutateAsync(data)
    setShowRestartModal(true)
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <LoadingSpinner size="lg" />
      </div>
    )
  }

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>{t('settings.loadFailed')}</AlertTitle>
        <AlertDescription>
          {error instanceof Error ? error.message : t('common.error')}
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{t('settings.contentProcessing')}</CardTitle>
          <CardDescription>
            {t('settings.contentProcessingDesc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-3">
            <Label htmlFor="doc_engine">{t('settings.docEngine')}</Label>
            <Controller
              name="default_content_processing_engine_doc"
              control={control}
              render={({ field }) => (
                  <Select
                    key={field.value}
                    name={field.name}
                    value={field.value || ''}
                    onValueChange={field.onChange}
                    disabled={field.disabled || isLoading}
                  >
                      <SelectTrigger id="doc_engine" className="w-full">
                        <SelectValue placeholder={t('settings.docEnginePlaceholder')} />
                      </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">{t('settings.autoRecommended')}</SelectItem>
                      <SelectItem value="docling" disabled={!doclingAvailable}>{t('settings.docling')}</SelectItem>
                      <SelectItem value="simple">{t('settings.simple')}</SelectItem>
                    </SelectContent>
                  </Select>
              )}
            />
            {!doclingAvailable && (
              <p className="text-sm text-muted-foreground">{t('settings.enableDoclingHint')}</p>
            )}
            <Collapsible open={expandedSections.doc} onOpenChange={() => toggleSection('doc')}>
              <CollapsibleTrigger className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors">
                <ChevronDownIcon className={`h-4 w-4 transition-transform ${expandedSections.doc ? 'rotate-180' : ''}`} />
                {t('settings.helpMeChoose')}
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-2 text-sm text-muted-foreground space-y-2">
                <p>{t('settings.docHelp')}</p>
              </CollapsibleContent>
            </Collapsible>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Controller
                name="docling_ocr"
                control={control}
                render={({ field }) => (
                  <Checkbox
                    id="docling_ocr"
                    checked={field.value ?? true}
                    onCheckedChange={field.onChange}
                    disabled={field.disabled || isLoading || !doclingAvailable}
                  />
                )}
              />
              <Label htmlFor="docling_ocr">{t('settings.ocrEnabled')}</Label>
            </div>
            <p className="text-sm text-muted-foreground">{t('settings.ocrHelp')}</p>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Controller
                name="docling_formulas"
                control={control}
                render={({ field }) => (
                  <Checkbox
                    id="docling_formulas"
                    checked={field.value ?? false}
                    onCheckedChange={field.onChange}
                    disabled={field.disabled || isLoading || !doclingAvailable}
                  />
                )}
              />
              <Label htmlFor="docling_formulas">{t('settings.formulasEnabled')}</Label>
            </div>
            <p className="text-sm text-muted-foreground">{t('settings.formulasHelp')}</p>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Controller
                name="docling_vision"
                control={control}
                render={({ field }) => (
                  <Checkbox
                    id="docling_vision"
                    checked={field.value ?? false}
                    onCheckedChange={field.onChange}
                    disabled={field.disabled || isLoading || !doclingAvailable}
                  />
                )}
              />
              <Label htmlFor="docling_vision">{t('settings.visionEnabled')}</Label>
            </div>
            <p className="text-sm text-muted-foreground">{t('settings.visionHelp')}</p>
          </div>

          <div className="space-y-3">
            <Label htmlFor="url_engine">{t('settings.urlEngine')}</Label>
            <Controller
              name="default_content_processing_engine_url"
              control={control}
              render={({ field }) => (
                <Select
                  key={field.value}
                  name={field.name}
                  value={field.value || ''}
                  onValueChange={field.onChange}
                  disabled={field.disabled || isLoading}
                >
                  <SelectTrigger id="url_engine" className="w-full">
                    <SelectValue placeholder={t('settings.urlEnginePlaceholder')} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">{t('settings.autoRecommended')}</SelectItem>
                    <SelectItem value="firecrawl">{t('settings.firecrawl')}</SelectItem>
                    <SelectItem value="jina">{t('settings.jina')}</SelectItem>
                    <SelectItem value="crawl4ai" disabled={!crawl4aiAvailable}>{t('settings.crawl4ai')}</SelectItem>
                    <SelectItem value="simple">{t('settings.simple')}</SelectItem>
                  </SelectContent>
                </Select>
              )}
            />
            {!crawl4aiAvailable && (
              <p className="text-sm text-muted-foreground">{t('settings.enableCrawl4aiHint')}</p>
            )}
             <Collapsible open={expandedSections.url} onOpenChange={() => toggleSection('url')}>
              <CollapsibleTrigger className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors">
                <ChevronDownIcon className={`h-4 w-4 transition-transform ${expandedSections.url ? 'rotate-180' : ''}`} />
                {t('settings.helpMeChoose')}
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-2 text-sm text-muted-foreground space-y-2">
                <p>{t('settings.urlHelp')}</p>
              </CollapsibleContent>
            </Collapsible>
          </div>
        </CardContent>
      </Card>

       <Card>
        <CardHeader>
          <CardTitle>{t('settings.embeddingAndSearch')}</CardTitle>
          <CardDescription>
            {t('settings.embeddingAndSearchDesc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
           <div className="space-y-3">
            <Label htmlFor="embedding">{t('settings.defaultEmbeddingOption')}</Label>
            <Controller
              name="default_embedding_option"
              control={control}
              render={({ field }) => (
                <Select
                  key={field.value}
                  name={field.name}
                  value={field.value || ''}
                  onValueChange={field.onChange}
                  disabled={field.disabled || isLoading}
                >
                  <SelectTrigger id="embedding" className="w-full">
                    <SelectValue placeholder={t('settings.embeddingOptionPlaceholder')} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ask">{t('settings.ask')}</SelectItem>
                    <SelectItem value="always">{t('settings.always')}</SelectItem>
                    <SelectItem value="never">{t('settings.never')}</SelectItem>
                  </SelectContent>
                </Select>
              )}
            />
             <Collapsible open={expandedSections.embedding} onOpenChange={() => toggleSection('embedding')}>
              <CollapsibleTrigger className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors">
                <ChevronDownIcon className={`h-4 w-4 transition-transform ${expandedSections.embedding ? 'rotate-180' : ''}`} />
                {t('settings.helpMeChoose')}
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-2 text-sm text-muted-foreground space-y-2">
                <p>{t('settings.embeddingHelp')}</p>
              </CollapsibleContent>
            </Collapsible>
          </div>
        </CardContent>
      </Card>

       <Card>
        <CardHeader>
          <CardTitle>{t('settings.fileManagement')}</CardTitle>
          <CardDescription>
            {t('settings.fileManagementDesc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
           <div className="space-y-3">
            <Label htmlFor="auto_delete">{t('settings.autoDeleteFiles')}</Label>
            <Controller
              name="auto_delete_files"
              control={control}
              render={({ field }) => (
                <Select
                  key={field.value}
                  name={field.name}
                  value={field.value || ''}
                  onValueChange={field.onChange}
                  disabled={field.disabled || isLoading}
                >
                  <SelectTrigger id="auto_delete" className="w-full">
                    <SelectValue placeholder={t('settings.autoDeletePlaceholder')} />
                  </SelectTrigger>
                   <SelectContent>
                    <SelectItem value="yes">{t('common.yes')}</SelectItem>
                    <SelectItem value="no">{t('common.no')}</SelectItem>
                  </SelectContent>
                </Select>
              )}
            />
             <Collapsible open={expandedSections.files} onOpenChange={() => toggleSection('files')}>
              <CollapsibleTrigger className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors">
                <ChevronDownIcon className={`h-4 w-4 transition-transform ${expandedSections.files ? 'rotate-180' : ''}`} />
                {t('settings.helpMeChoose')}
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-2 text-sm text-muted-foreground space-y-2">
                <p>{t('settings.filesHelp')}</p>
              </CollapsibleContent>
            </Collapsible>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.allowLocalProcessing')}</CardTitle>
          <CardDescription>{t('settings.allowLocalProcessingDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Controller
                name="allow_local_processing"
                control={control}
                render={({ field }) => (
                  <Checkbox
                    id="allow_local_processing"
                    checked={field.value ?? false}
                    onCheckedChange={(val) => {
                      field.onChange(val as boolean)
                      setShowRestartModal(true)
                      setValue('allow_local_processing', val as boolean)
                    }}
                    disabled={field.disabled || isLoading}
                  />
                )}
              />
              <Label htmlFor="allow_local_processing">{t('settings.allowLocalProcessing')}</Label>
            </div>
            {allowLocalProcessing === false && (
              <p className="text-sm text-muted-foreground">
                {t('settings.localProcessingDisabledNotice')}
              </p>
            )}
            {allowLocalProcessing === true && (
              <p className="text-sm text-muted-foreground">
                {t('settings.localProcessingEnabledNotice')}
              </p>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.sessionStatus')}</CardTitle>
          <CardDescription>{t('settings.sessionStatusDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span>{t('settings.sessionId')}</span>
              <span className="text-muted-foreground truncate max-w-xs">
                {sessionData.session_id || t('settings.sessionNotInitialized')}
              </span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="w-24">{t('settings.localProcessing')}</span>
              <span className={[
                'px-2 py-0.5 rounded text-xs font-medium',
                sessionData.local_processing_enabled
                  ? 'bg-green-100 text-green-700'
                  : 'bg-gray-100 text-gray-600',
              ].join(' ')}>
                {sessionData.local_processing_enabled
                  ? t('settings.enabled')
                  : t('settings.disabled')}
              </span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="w-24">{t('settings.cloudProcessing')}</span>
              <span className={[
                'px-2 py-0.5 rounded text-xs font-medium',
                sessionData.cloud_processing_enabled !== false
                  ? 'bg-green-100 text-green-700'
                  : 'bg-gray-100 text-gray-600',
              ].join(' ')}>
                {sessionData.cloud_processing_enabled !== false
                  ? t('settings.enabled')
                  : t('settings.disabled')}
              </span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="w-24">{t('settings.browserSession')}</span>
              <span className={[
                'px-2 py-0.5 rounded text-xs font-medium',
                sessionData.browser_session_active
                  ? 'bg-green-100 text-green-700'
                  : 'bg-gray-100 text-gray-600',
              ].join(' ')}>
                {sessionData.browser_session_active
                  ? t('settings.active')
                  : t('settings.inactive')}
              </span>
            </div>
            {sessionData.last_refresh_at && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>{t('settings.lastRefreshed')}</span>
                <span>{new Date(sessionData.last_refresh_at).toLocaleString()}</span>
              </div>
            )}
            <Button
              onClick={async () => {
                setSessionLoading(true)
                try {
                  await sessionApi.refresh()
                  const data = await sessionApi.getStatus()
                  setSessionData(data)
                } finally {
                  setSessionLoading(false)
                }
              }}
              disabled={sessionLoading}
              size="sm"
            >
              {sessionLoading ? t('common.refreshing') : t('settings.refreshSession')}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Quota Status Card */}
      <Card>
        <CardHeader>
          <CardTitle>{t('settings.quotaStatus')}</CardTitle>
          <CardDescription>{t('settings.quotaStatusDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span>{t('settings.cumulativeUsed')}</span>
              <span className={[
                'font-mono text-xs',
                quotaData.cumulative_exceeded ? 'text-destructive' : 'text-muted-foreground',
              ].join(' ')}>
                {quotaData.cumulative_used}
              </span>
              <span className="text-xs text-muted-foreground">/ {quotaData.cumulative_limit || '∞'}</span>
            </div>
            {quotaData.cumulative_exceeded && (
              <div className="flex items-center gap-2 text-xs text-destructive">
                <span className="h-3 w-3 rounded-full bg-destructive/20 text-destructive" />
                <span>{t('settings.quotaExceeded')}</span>
              </div>
            )}
            {Object.entries(quotaData.providers || {}).length > 0 && (
              <div className="space-y-2">
                {Object.entries(quotaData.providers).map(([provider, stats]) => (
                  <div key={provider} className="flex items-center justify-between text-sm border-b pb-1 last:border-b-0">
                    <span className="font-mono text-xs">{provider}</span>
                    <div className="flex items-center gap-2">
                      <span className={[
                        'px-1.5 py-0.5 rounded text-xs font-mono',
                        stats.exceeded ? 'bg-destructive/10 text-destructive' : 'bg-green-100 text-green-700',
                      ].join(' ')}>
                        {stats.used}/{stats.limit || '∞'}
                      </span>
                      {stats.remaining !== undefined && stats.remaining !== Infinity && (
                        <span className="text-xs text-muted-foreground">{t('settings.remaining', { count: stats.remaining })}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {quotaData.updated_at && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>{t('settings.lastRefreshed')}</span>
                <span>{new Date(quotaData.updated_at).toLocaleString()}</span>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* DeepSeek-web Credentials Card */}
      <Card>
        <CardHeader>
          <CardTitle>{t('settings.deepseekWebTitle')}</CardTitle>
          <CardDescription>{t('settings.deepseekWebDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <div className="flex items-center justify-between text-sm">
              <span>{t('settings.deepseekConfigured')}</span>
              <span className={[
                'px-2 py-0.5 rounded text-xs font-medium',
                deepseekConfigured
                  ? 'bg-green-100 text-green-700'
                  : 'bg-gray-100 text-gray-600',
              ].join(' ')}>
                {deepseekConfigured ? t('settings.configured') : t('settings.notConfigured')}
              </span>
            </div>

            {deepseekConfigured ? (
              <div className="space-y-3">
                <Button
                  onClick={async () => {
                    setDeepseekLoading(true)
                    try {
                      await deepseekApi.refresh()
                      setDeepseekConfigured(false)
                      setConfigureMessage(null)
                      setConfigureError(null)
                    } catch {
                      // ignore
                    } finally {
                      setDeepseekLoading(false)
                    }
                  }}
                  disabled={deepseekLoading}
                  size="sm"
                  variant="outline"
                >
                  {deepseekLoading ? t('common.refreshing') : t('settings.deepseekClearCredentials')}
                </Button>
                <p className="text-xs text-muted-foreground">
                  {t('settings.deepseekClearDesc')}
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="space-y-2">
                  <Label htmlFor="ds_session_id">{t('settings.deepseekSessionId')}</Label>
                  <input
                    id="ds_session_id"
                    type="text"
                    value={dsSessionId}
                    onChange={(e) => setDsSessionId(e.target.value)}
                    placeholder={t('settings.deepseekSessionIdPlaceholder')}
                    className="w-full border rounded px-3 py-2 text-sm bg-input disabled:opacity-50"
                    disabled={deepseekLoading}
                  />
                  <p className="text-xs text-muted-foreground">
                    {t('settings.deepseekSessionIdHelp')}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="auth_token">{t('settings.deepseekAuthToken')}</Label>
                  <input
                    id="auth_token"
                    type="text"
                    value={authToken}
                    onChange={(e) => setAuthToken(e.target.value)}
                    placeholder={t('settings.deepseekAuthTokenPlaceholder')}
                    className="w-full border rounded px-3 py-2 text-sm bg-input disabled:opacity-50"
                    disabled={deepseekLoading}
                  />
                  <p className="text-xs text-muted-foreground">
                    {t('settings.deepseekAuthTokenHelp')}
                  </p>
                </div>
                {configureError && (
                  <Alert variant="destructive">
                    <AlertTitle>{t('settings.deepseekConfigError')}</AlertTitle>
                    <AlertDescription>{configureError}</AlertDescription>
                  </Alert>
                )}
                {configureMessage && (
                  <Alert variant="default">
                    <AlertTitle>{t('settings.deepseekConfigSuccess')}</AlertTitle>
                    <AlertDescription>{configureMessage}</AlertDescription>
                  </Alert>
                )}
                <Button
                  onClick={async () => {
                    setConfigureError(null)
                    setConfigureMessage(null)
                    if (!dsSessionId || !authToken) {
                      setConfigureError(t('settings.deepseekBothFieldsRequired'))
                      return
                    }
                    setDeepseekLoading(true)
                    try {
                      const result = await deepseekApi.configure({
                        ds_session_id: dsSessionId,
                        authorization_token: authToken,
                      })
                      setConfigureMessage(result.message)
                      setDeepseekConfigured(true)
                      setDsSessionId('')
                      setAuthToken('')
                    } catch (e) {
                      const err = e as { response?: { data?: { detail?: string } } };
                      setConfigureError(err?.response?.data?.detail || t('settings.deepseekConfigFailed'))
                    } finally {
                      setDeepseekLoading(false)
                    }
                  }}
                  disabled={deepseekLoading || !dsSessionId || !authToken}
                >
                  {deepseekLoading ? t('common.saving') : t('settings.deepseekConfigure')}
                </Button>
              </div>
            )}

            {deepseekStatus?.pow_required && (
              <Alert variant="destructive">
                <AlertTitle>{t('settings.deepseekPowRequired')}</AlertTitle>
                <AlertDescription>{t('settings.deepseekPowRequiredDesc')}</AlertDescription>
              </Alert>
            )}
            {!deepseekConfigured && !deepseekStatus?.pow_required && (
              <Alert variant="default">
                <AlertTitle>{t('settings.deepseekSetupNeeded')}</AlertTitle>
                <AlertDescription>{t('settings.deepseekSetupNeededDesc')}</AlertDescription>
              </Alert>
            )}
          </div>
        </CardContent>
      </Card>

      {showRestartModal && (
        <Alert variant="default">
          <AlertTitle>{t('settings.restartRequiredTitle')}</AlertTitle>
          <AlertDescription>{t('settings.restartRequiredDesc')}</AlertDescription>
          <div className="flex gap-2 justify-end">
            <Button onClick={() => setShowRestartModal(false)} variant="outline">
              {t('common.cancel')}
            </Button>
            <Button onClick={() => window.location.reload()} variant="default">
              {t('common.restart')}
            </Button>
          </div>
        </Alert>
      )}

      <div className="flex justify-end">
         <Button 
          type="submit" 
          disabled={!isDirty || updateSettings.isPending}
        >
          {updateSettings.isPending ? t('common.saving') : t('common.save')}
        </Button>
      </div>
    </form>
  )
}
