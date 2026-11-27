"use client"

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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useSettings, useUpdateSettings } from '@/lib/hooks/use-settings'
import { useCapabilities } from '@/lib/hooks/use-capabilities'
import {
  sessionApi,
  quotaApi,
  deepseekApi,
  processingFlags,
  ollamaApi,
  syncApi,
  ttsSttApi,
  preprocessorApi,
  browserAiApi,
} from '@/lib/api/settings'
import {
  DeepSeekSessionStatus,
  SupportedLanguages,
} from '@/lib/types/api'
import { useCallback, useEffect, useState } from 'react'
import { ChevronDownIcon, Loader2, X } from 'lucide-react'
import { useTranslation } from '@/lib/hooks/use-translation'
import { useOfflineStore } from '@/lib/offline-store'
import { SystemInfo } from './advanced/SystemInfo'
import { ProcessingQueue } from './advanced/ProcessingQueue'
import { RebuildEmbeddings } from './advanced/RebuildEmbeddings'
import { QueueBacklogPanel } from '@/components/processing/QueueBacklogPanel'
import { CapabilitiesCard } from '@/components/processing/CapabilitiesCard'

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
  const { cacheUsage, cacheUsagePercentage, isLoading: cacheLoading, clearCache, refreshStorage } = useOfflineStore()

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
  const [ttsSttStatus, setTtsSttStatus] = useState<{
    allow_local_processing: boolean
    piper_reachable: boolean
    piper_detail: string
  } | null>(null)
  const [sttStatus, setSttStatus] = useState<{
    allow_local_processing: boolean
    whisper_reachable: boolean
    whisper_detail: string
  } | null>(null)
  const [supportedLanguages, setSupportedLanguages] = useState<SupportedLanguages | null>(null)
  const [ttsSttLoading, setTtsSttLoading] = useState(false)
  const [preprocessGroups, setPreprocessGroups] = useState<Record<string, boolean>>({})
  const [ttsProvider, setTtsProvider] = useState('browser')
  const [sttProvider, setSttProvider] = useState('browser')
  const [ttsLanguage, setTtsLanguage] = useState('')
  const [sttLanguage, setSttLanguage] = useState('')
  const [browserAiLoading, setBrowserAiLoading] = useState(false)
  const [googleAiModeUrl, setGoogleAiModeUrl] = useState('http://127.0.0.1:18080/v1')
  const [lightpandaCdpUrl, setLightpandaCdpUrl] = useState('http://127.0.0.1:9222')
  const [geminiApiKey, setGeminiApiKey] = useState('')
  const [duckAiUrl, setDuckAiUrl] = useState('http://127.0.0.1:8765/v1')
  const [browserAiConfigureMessage, setBrowserAiConfigureMessage] = useState<string | null>(null)
  const [browserAiConfigureError, setBrowserAiConfigureError] = useState<string | null>(null)
  const [browserAiStatus, setBrowserAiStatus] = useState<{
    google_ai_mode: { configured: boolean; url?: string | null; playwright_fallback: boolean }
    duck_ai: { configured: boolean; url?: string | null; p2d_duck_available: boolean }
    rate_limits: { duck_ai: string; google_ai_mode: string }
  } | null>(null)
  const [deepseekStatus, setDeepseekStatus] = useState<DeepSeekSessionStatus | null>(null)
  const [deepseekLoading, setDeepseekLoading] = useState(false)
  const [deepseekConfigured, setDeepseekConfigured] = useState(false)
  const [dsSessionId, setDsSessionId] = useState('')
  const [authToken, setAuthToken] = useState('')
  const [configureMessage, setConfigureMessage] = useState<string | null>(null)
  const [configureError, setConfigureError] = useState<string | null>(null)
  const [deepSeekPcUrl, setDeepSeekPcUrl] = useState('')
  const [duckAiDefaultModel, setDuckAiDefaultModel] = useState('gpt-6-luna')
  const [duckAiConfigured, setDuckAiConfigured] = useState(true)
  const [googleAiModeConfigured, setGoogleAiModeConfigured] = useState(true)
  // Ollama local LLM
  const [ollamaUrl, setOllamaUrl] = useState('')
  const [ollamaDefaultModel, setOllamaDefaultModel] = useState('llama3.2:3b')
  const [ollamaConfigured, setOllamaConfigured] = useState(false)
  const [ollamaModels, setOllamaModels] = useState<string[]>([])
  const [ollamaLoadedModels, setOllamaLoadedModels] = useState<string[]>([])
  const [ollamaPulling, setOllamaPulling] = useState(false)
  const [ollamaUnloading, setOllamaUnloading] = useState(false)
  // Sync configuration
  const [syncDir, setSyncDir] = useState('')
  const [syncToken, setSyncToken] = useState('')
  const [syncPairedDevices, setSyncPairedDevices] = useState<string[]>([])
  const [syncAutoSync, setSyncAutoSync] = useState(false)
  const [syncUserspaceOnly, setSyncUserspaceOnly] = useState(true)
  const [syncNotebooks, setSyncNotebooks] = useState(false)
  const [syncSessions, setSyncSessions] = useState(false)
  const [syncAnalyticsEnabled, setSyncAnalyticsEnabled] = useState(false)
  const [syncConflictResolution, setSyncConflictResolution] = useState('last_write_wins')
  const [syncLastSyncAt, setSyncLastSyncAt] = useState<string | null>(null)
  const [newSyncDevice, setNewSyncDevice] = useState('')
  // Sync conflicts
  const [syncConflicts, setSyncConflicts] = useState<{ collection: string; db_record_id: string; local_updated: string | null; remote_updated: string | null }[]>([])
  const [showConflictDialog, setShowConflictDialog] = useState(false)
  // Session-level settings
  const [sessionCloudLocalSwitching, setSessionCloudLocalSwitching] = useState(true)
  const [sessionAutoCompactionThreshold, setSessionAutoCompactionThreshold] = useState(0.7)
  const [sessionAutoCompactionHardLimit, setSessionAutoCompactionHardLimit] = useState(0.95)
  const [sessionHighPriorityTimeoutDays, setSessionHighPriorityTimeoutDays] = useState(30)
  const [sessionLazyLoadEnabled, setSessionLazyLoadEnabled] = useState(true)

  // Ollama model management callbacks (defined before useEffect to satisfy
  // React Rules of Hooks - all hooks must be called in the same order)
  const fetchOllamaModels = useCallback(async () => {
    if (!ollamaUrl) return
    try {
      const baseUrl = ollamaUrl.replace(/\/$/, '')
      const response = await fetch(`${baseUrl}/api/tags`)
      if (response.ok) {
        const data = await response.json()
        const models = (data.models || []).map((m: any) => m.name)
        setOllamaModels(models)
      }
    } catch {
      // Ignore - Ollama may not be running
    }
  }, [ollamaUrl])

  const fetchOllamaLoadedModels = useCallback(async () => {
    if (!ollamaUrl) return
    try {
      const data = await ollamaApi.listLoaded()
      setOllamaLoadedModels((data.models || []).map((m: any) => m.name))
    } catch {
      // Ignore
    }
  }, [ollamaApi, ollamaUrl])

  const pullOllamaModel = useCallback(async (model: string) => {
    setOllamaPulling(true)
    try {
      await ollamaApi.pullModel(model)
      setConfigureMessage(`Model ${model} pulled successfully`)
      await fetchOllamaModels()
      await fetchOllamaLoadedModels()
    } catch (e: any) {
      setConfigureError(e.response?.data?.detail || e.message || `Failed to pull ${model}`)
    } finally {
      setOllamaPulling(false)
    }
  }, [ollamaApi, fetchOllamaModels, fetchOllamaLoadedModels])

  const unloadOllamaModel = useCallback(async (model: string) => {
    setOllamaUnloading(true)
    try {
      await ollamaApi.unloadModel(model)
      setConfigureMessage(`Model ${model} unloaded`)
      await fetchOllamaLoadedModels()
    } catch (e: any) {
      setConfigureError(e.response?.data?.detail || e.message || `Failed to unload ${model}`)
    } finally {
      setOllamaUnloading(false)
    }
  }, [ollamaApi, fetchOllamaLoadedModels])

  const ensureOllamaLoaded = useCallback(async (model: string) => {
    setOllamaPulling(true)
    try {
      const result = await ollamaApi.ensureLoaded(model, true)
      setConfigureMessage(`Model ${model} ${result.status}`)
      await fetchOllamaLoadedModels()
    } catch (e: any) {
      setConfigureError(e.response?.data?.detail || e.message || `Failed to load ${model}`)
    } finally {
      setOllamaPulling(false)
    }
  }, [ollamaApi, fetchOllamaLoadedModels])

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
    const fetchTtsStt = async () => {
      setTtsSttLoading(true)
      try {
        const [tts, stt, l] = await Promise.all([
          ttsSttApi.ttsStatus(),
          ttsSttApi.sttStatus(),
          ttsSttApi.languages(),
        ])
        if (mounted) {
          setTtsSttStatus(tts as unknown as { allow_local_processing: boolean; piper_reachable: boolean; piper_detail: string })
          setSttStatus(stt as unknown as { allow_local_processing: boolean; whisper_reachable: boolean; whisper_detail: string })
          setSupportedLanguages(l)
        }
      } catch {
        if (mounted) setTtsSttLoading(false)
      } finally {
        if (mounted) setTtsSttLoading(false)
      }
    }
    const fetchPreprocessGroups = async () => {
      try {
        const g = await preprocessorApi.groups()
        if (mounted) setPreprocessGroups(g.groups)
      } catch {
        // ignore
      }
    }
    const fetchBrowserAi = async () => {
      setBrowserAiLoading(true)
      try {
        const data = await browserAiApi.status()
        if (mounted) {
          setBrowserAiStatus(data)
          // Populate URL fields from status if available
          if (data.google_ai_mode.url) {
            setGoogleAiModeUrl(data.google_ai_mode.url)
          }
          if (data.duck_ai.url) {
            setDuckAiUrl(data.duck_ai.url)
          }
        }
      } catch {
        if (mounted) setBrowserAiLoading(false)
      } finally {
        if (mounted) setBrowserAiLoading(false)
      }
    }
    fetchDeepseek()
    const fetchProcessingFlags = async () => {
      try {
        const data = await processingFlags.get()
        if (mounted) {
          setDeepSeekPcUrl(data.deepseek_pc_url ?? '')
          setGoogleAiModeUrl(data.google_ai_mode_url ?? '')
          setLightpandaCdpUrl(data.lightpanda_cdp_url ?? '')
          setGeminiApiKey(data.gemini_api_key ?? '')
          setDuckAiUrl(data.duck_ai_url ?? '')
          setDuckAiDefaultModel(data.duck_ai_default_model ?? 'gpt-6-luna')
          setOllamaUrl(data.ollama_url ?? '')
          setOllamaDefaultModel(data.ollama_default_model ?? 'llama3.2:3b')
          setGoogleAiModeConfigured(
            !!data.google_ai_mode_url
            || !!data.lightpanda_cdp_url
            || !!data.gemini_api_key
          )
          setOllamaConfigured(!!data.ollama_url)
          // Sync configuration
          setSyncDir(data.sync_dir ?? '')
          setSyncToken(data.sync_token ?? '')
          setSyncPairedDevices(data.sync_paired_devices ?? [])
          setSyncAutoSync(data.sync_auto_sync ?? false)
          setSyncUserspaceOnly(data.sync_userspace_only ?? true)
          setSyncNotebooks(data.sync_notebooks ?? false)
          setSyncSessions(data.sync_sessions ?? false)
          setSyncAnalyticsEnabled(data.sync_analytics_enabled ?? false)
          setSyncConflictResolution(data.sync_conflict_resolution ?? 'last_write_wins')
          setSyncLastSyncAt(data.sync_last_sync_at ?? null)
          // Preprocessor group settings
          setPreprocessGroups({
            documents: data.preprocess_documents ?? true,
            audio: data.preprocess_audio ?? true,
            image: data.preprocess_image ?? true,
            video: data.preprocess_video ?? true,
            forceEnabled: data.preprocess_force_enabled ?? true,
          })
          // Session-level settings
          setSessionCloudLocalSwitching(data.session_cloud_local_switching ?? true)
          setSessionAutoCompactionThreshold(data.session_auto_compaction_threshold ?? 0.7)
          setSessionAutoCompactionHardLimit(data.session_auto_compaction_hard_limit ?? 0.95)
          setSessionHighPriorityTimeoutDays(data.session_high_priority_timeout_days ?? 30)
          setSessionLazyLoadEnabled(data.session_lazy_load_enabled ?? true)
          setDuckAiConfigured(!!data.duck_ai_url)
        }
      } catch {
        // Non-critical; PC fallback URL is optional
      }
    }
    fetchProcessingFlags()
    // Fetch available Ollama models from the local server
    fetchOllamaModels()
    // Fetch currently loaded Ollama models
    fetchOllamaLoadedModels()
    fetchTtsStt()
    fetchPreprocessGroups()
    fetchBrowserAi()
    refreshStorage()
    return () => { mounted = false }
  }, [refreshStorage, ollamaUrl, fetchOllamaModels, fetchOllamaLoadedModels])


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
    // Save preprocessor group settings
    try {
      await processingFlags.update({
        preprocess_documents: preprocessGroups.documents,
        preprocess_audio: preprocessGroups.audio,
        preprocess_image: preprocessGroups.image,
        preprocess_video: preprocessGroups.video,
        preprocess_force_enabled: preprocessGroups.forceEnabled,
      })
    } catch {
      // Non-critical; preprocessor settings will retry on next save
    }
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
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      <Tabs defaultValue="general" className="w-full">
        <TabsList className="w-full justify-center border-b">
          <TabsTrigger value="general" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabGeneral')}
          </TabsTrigger>
          <TabsTrigger value="tts-stt" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabTtsStt')}
          </TabsTrigger>
          <TabsTrigger value="preprocessor" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabPreprocessor')}
          </TabsTrigger>
          <TabsTrigger value="cache-offline" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabCacheOffline')}
          </TabsTrigger>
          <TabsTrigger value="rate-limits" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabRateLimits')}
          </TabsTrigger>
          <TabsTrigger value="sync" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabSync')}
          </TabsTrigger>
          <TabsTrigger value="advanced" className="px-4 py-2 text-sm font-medium">
            {t('settings.tabAdvanced')}
          </TabsTrigger>
        </TabsList>

        {/* General Tab — Content Processing + File Management */}
        <TabsContent value="general" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t('settings.contentProcessing')}</CardTitle>
              <CardDescription>{t('settings.contentProcessingDesc')}</CardDescription>
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
              <CardTitle>{t('settings.fileManagement')}</CardTitle>
              <CardDescription>{t('settings.fileManagementDesc')}</CardDescription>
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

        {/* Embedding & Search */}
        <Card>
          <CardHeader>
            <CardTitle>{t('settings.embeddingAndSearch')}</CardTitle>
            <CardDescription>{t('settings.embeddingAndSearchDesc')}</CardDescription>
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
        </TabsContent>

        {/* TTS/STT Tab */}
        <TabsContent value="tts-stt" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t('settings.ttsSttTitle')}</CardTitle>
              <CardDescription>{t('settings.ttsSttDesc')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {/* TTS */}
                <div className="space-y-3">
                  <Label>{t('settings.ttsProvider')}</Label>
                  <Select
                    value={ttsProvider}
                    onValueChange={(val) => setTtsProvider(val)}
                    disabled={ttsSttLoading}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder={t('settings.ttsProviderPlaceholder')} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="piper">{t('settings.ttsProviderPiper')}</SelectItem>
                      <SelectItem value="edge-tts">{t('settings.ttsProviderEdgeTts')}</SelectItem>
                      <SelectItem value="browser">{t('settings.ttsProviderBrowser')}</SelectItem>
                    </SelectContent>
                  </Select>
                  <div className="space-y-2">
                    <Label>{t('settings.ttsLanguage')}</Label>
                    <Select
                      value={ttsLanguage}
                      onValueChange={(val) => setTtsLanguage(val)}
                      disabled={ttsSttLoading}
                    >
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder={t('settings.ttsLanguagePlaceholder')} />
                      </SelectTrigger>
                      <SelectContent>
                        {(supportedLanguages?.supported_small_tier ?? []).map((lang) => (
                          <SelectItem key={lang} value={lang}>
                            {lang.toUpperCase()}
                          </SelectItem>
                        ))}
                        {(supportedLanguages?.supported_large_tier ?? []).map((lang) => (
                          <SelectItem key={lang} value={lang}>
                            {lang.toUpperCase()}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                {/* STT */}
                <div className="space-y-3">
                  <Label>{t('settings.sttProvider')}</Label>
                  <Select
                    value={sttProvider}
                    onValueChange={(val) => setSttProvider(val)}
                    disabled={ttsSttLoading}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder={t('settings.sttProviderPlaceholder')} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="whisper.cpp">{t('settings.sttProviderWhisperCpp')}</SelectItem>
                      <SelectItem value="browser">{t('settings.sttProviderBrowser')}</SelectItem>
                    </SelectContent>
                  </Select>
                  <div className="space-y-2">
                    <Label>{t('settings.sttLanguage')}</Label>
                    <Select
                      value={sttLanguage}
                      onValueChange={(val) => setSttLanguage(val)}
                      disabled={ttsSttLoading}
                    >
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder={t('settings.sttLanguagePlaceholder')} />
                      </SelectTrigger>
                      <SelectContent>
                        {(supportedLanguages?.supported_small_tier ?? []).map((lang) => (
                          <SelectItem key={lang} value={lang}>
                            {lang.toUpperCase()}
                          </SelectItem>
                        ))}
                        {(supportedLanguages?.supported_large_tier ?? []).map((lang) => (
                          <SelectItem key={lang} value={lang}>
                            {lang.toUpperCase()}
                          </SelectItem>
                        ))}
                        {(supportedLanguages?.unsupported ?? []).map((lang) => (
                          <SelectItem key={lang} value={lang} disabled>
                            {lang.toUpperCase()} (unsupported)
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="rounded-lg bg-muted p-3">
                  <p className="text-sm text-muted-foreground">
                    {t('settings.ttsSttNotice')}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {t('settings.allowLocalProcessingForTtsStt')}
                  </p>
                </div>

                {ttsSttLoading || sttStatus ? (
                  <div className="flex items-center justify-between text-sm border-t pt-2">
                    <span>{t('settings.localProcessing')}</span>
                    <span className={[
                      'px-2 py-0.5 rounded text-xs font-medium',
                      ttsSttStatus?.allow_local_processing
                        ? 'bg-fern-tint text-fern-deep'
                        : 'bg-muted text-muted-foreground',
                    ].join(' ')}>
                      {ttsSttStatus?.allow_local_processing ? t('settings.enabled') : t('settings.disabled')}
                    </span>
                  </div>
                ) : null}
              </div>
            </CardContent>
          </Card>
        </TabsContent>
        {/* Preprocessor Tab */}
        <TabsContent value="preprocessor" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t('settings.preprocessorTitle')}</CardTitle>
              <CardDescription>{t('settings.preprocessorDesc')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label htmlFor="preprocess_documents">
                      {t('settings.preprocessorDocuments')}
                    </Label>
                    <Checkbox
                      id="preprocess_documents"
                      checked={preprocessGroups.documents ?? true}
                      onCheckedChange={(val) => setPreprocessGroups(prev => ({ ...prev, documents: val as boolean }))}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <Label htmlFor="preprocess_audio">
                      {t('settings.preprocessorAudio')}
                    </Label>
                    <Checkbox
                      id="preprocess_audio"
                      checked={preprocessGroups.audio ?? true}
                      onCheckedChange={(val) => setPreprocessGroups(prev => ({ ...prev, audio: val as boolean }))}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <Label htmlFor="preprocess_image">
                      {t('settings.preprocessorImage')}
                    </Label>
                    <Checkbox
                      id="preprocess_image"
                      checked={preprocessGroups.image ?? true}
                      onCheckedChange={(val) => setPreprocessGroups(prev => ({ ...prev, image: val as boolean }))}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <Label htmlFor="preprocess_video">
                      {t('settings.preprocessorVideo')}
                    </Label>
                    <Checkbox
                      id="preprocess_video"
                      checked={preprocessGroups.video ?? true}
                      onCheckedChange={(val) => setPreprocessGroups(prev => ({ ...prev, video: val as boolean }))}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <Label htmlFor="preprocess_force">
                      {t('settings.preprocessorForceEnabled')}
                    </Label>
                    <Checkbox
                      id="preprocess_force"
                      checked={preprocessGroups.forceEnabled ?? true}
                      onCheckedChange={(val) => setPreprocessGroups(prev => ({ ...prev, forceEnabled: val as boolean }))}
                    />
                  </div>
                </div>
                <div className="rounded-lg bg-info/10 p-3">
                  <p className="text-sm text-muted-foreground">
                    {t('settings.preprocessorForceDesc')}
                  </p>
                </div>
                <div className="rounded-lg bg-info/10 p-3">
                  <p className="text-sm text-muted-foreground">
                    {t('settings.preprocessorNotice')}
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Cache & Offline Tab */}
        <TabsContent value="cache-offline" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t('settings.cacheOfflineTitle')}</CardTitle>
              <CardDescription>{t('settings.cacheOfflineDesc')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {/* Offline status */}
                <div className="flex items-center gap-3 px-4 py-3 bg-muted rounded-lg">
                  <span className="w-2 h-2 rounded-full" 
                    style={{ backgroundColor: typeof navigator !== 'undefined' && navigator.onLine ? 'var(--color-offline-chip-text-online)' : 'var(--color-cache-removable)' }} />
                  <span className="text-sm font-medium">
                    {typeof navigator !== 'undefined' && navigator.onLine ? t('settings.online') : t('settings.offline')}
                  </span>
                </div>

                {/* Cache usage */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-sm">
                    <span>{t('settings.cacheUsage')}</span>
                    <span className="font-mono text-xs text-muted-foreground">
                      {cacheUsagePercentage}% ({(cacheUsage / 1024 / 1024).toFixed(1)} MB)
                    </span>
                  </div>
                  <div className="w-full bg-surface-recessed rounded-full h-2">
                    <div 
                      className="h-2 rounded-full bg-primary transition-all duration-300"
                      style={{ width: `${Math.min(cacheUsagePercentage, 100)}%` }}
                    />
                  </div>
                </div>

                {/* Clear cache */}
                <div className="bg-destructive-tint/50 border border-destructive/20 rounded-lg p-4">
                  <div className="flex items-start gap-3">
                    <span className="w-5 h-5 text-destructive">⚠️</span>
                    <div>
                      <h3 className="text-sm font-semibold text-destructive">{t('settings.clearCacheTitle')}</h3>
                      <p className="text-xs text-muted-foreground">{t('settings.clearCacheDesc')}</p>
                    </div>
                  </div>
                  <div className="mt-4 flex gap-2 justify-end">
                    <Button variant="outline" size="sm" onClick={() => {}}>
                      {t('common.cancel')}
                    </Button>
                    <Button variant="destructive" size="sm" onClick={clearCache} disabled={cacheLoading}>
                      {cacheLoading ? t('common.saving') : t('settings.clearCacheConfirm')}
                    </Button>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Rate Limits Tab */}
        <TabsContent value="rate-limits" className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t('settings.tabRateLimits')}</CardTitle>
              <CardDescription>{t('settings.rateLimitsDesc')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-6">
                <div className="rounded-lg bg-muted p-3">
                  <p className="text-sm">{t('settings.rateLimitOrderDesc')}</p>
                  <div className="mt-2 space-y-1">
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>1</span>
                      <span>{t('settings.rateLimitOrder1')}</span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>2</span>
                      <span>{t('settings.rateLimitOrder2')}</span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>3</span>
                      <span>{t('settings.rateLimitOrder3')}</span>
                    </div>
                  </div>
                </div>
                <div className="space-y-4">
                  {Object.entries(quotaData.providers).map(([provider, info]) => (
                    <div key={provider} className="border rounded-lg p-3">
                      <div className="flex items-center justify-between mb-2">
                        <span className="font-semibold capitalize">{provider}</span>
                        {info.exceeded && (
                          <span className="px-2 py-0.5 rounded text-xs bg-destructive/10 text-destructive">
                            {t('settings.rateLimitExceeded')}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center justify-between text-sm">
                        <span className="text-muted-foreground">{info.used} / {info.limit}</span>
                        <span className="font-mono text-xs">{info.limit > 0 ? Math.round(((info.used / info.limit) * 100)) : 0}%</span>
                      </div>
                      <div className="w-full bg-surface-recessed rounded-full h-1.5 mt-1">
                        <div className="h-1.5 rounded-full bg-primary transition-all" style={{ width: `${info.limit > 0 ? Math.min((info.used / info.limit) * 100, 100) : 0}%` }} />
                      </div>
                      {info.reset_at && (
                        <p className="text-xs text-muted-foreground mt-1">{t('settings.rateLimitResetsAt', { time: info.reset_at })}</p>
                      )}
                    </div>
                  ))}
                </div>
                <div className="rounded-lg border p-3">
                  <p className="text-sm font-medium mb-2">{t('settings.rateLimitRetryDesc')}</p>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>{t('settings.rateLimitRetryAttempts')}</span>
                    <span>10</span>
                  </div>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>{t('settings.rateLimitRetryBackoff')}</span>
                    <span>5s doubling, cap 60s</span>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
          <div className="rounded-lg border p-4">
            <div className="flex items-center justify-between mb-2">
              <Label htmlFor="auto_fallback">{t('settings.autoFallback')}</Label>
              <span className="text-xs text-muted-foreground">{t('settings.autoFallbackDesc')}</span>
            </div>
            <Checkbox id="auto_fallback" checked={false} onCheckedChange={() => {}} disabled />
          </div>
        </TabsContent>

        {/* Advanced Tab */}
        <TabsContent value="advanced" className="space-y-6">
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

          <SystemInfo />
          <ProcessingQueue />
          <QueueBacklogPanel />
          <CapabilitiesCard />
          <RebuildEmbeddings />
        </TabsContent>
      </Tabs>

      <div className="flex justify-end pt-4">
         <Button 
          type="submit" 
          disabled={!isDirty || updateSettings.isPending}
        >
          {updateSettings.isPending ? t('common.saving') : t('common.save')}
        </Button>
      </div>

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
    </form>
  )
}
