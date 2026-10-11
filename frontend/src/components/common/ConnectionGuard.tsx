'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { ConnectionError } from '@/lib/types/config'
import { ConnectionErrorOverlay } from '@/components/errors/ConnectionErrorOverlay'
import { getConfig, resetConfig } from '@/lib/config'
import { useTranslation } from '@/lib/hooks/use-translation'

interface ConnectionGuardProps {
  children: React.ReactNode
}

export function ConnectionGuard({ children }: ConnectionGuardProps) {
  const { t } = useTranslation()
  const [error, setError] = useState<ConnectionError | null>(null)
  const [isChecking, setIsChecking] = useState(true)
  // Draining is advisory, never blocking: the backend is hibernating but alive.
  const [draining, setDraining] = useState(false)
  const [waking, setWaking] = useState(false)
  const [wakeError, setWakeError] = useState<string | null>(null)
  // Use a ref to track checking status to avoid dependency cycles
  const isCheckingRef = useRef(false)

  const checkConnection = useCallback(async () => {
    // Prevent re-entry if already checking
    if (isCheckingRef.current) {
       return
    }
    
    isCheckingRef.current = true
    setIsChecking(true)
    
    setError(null)

    // Reset config cache to force a fresh fetch
    resetConfig()

    try {
      const config = await getConfig()

      // Check if database is offline
      if (config.dbStatus === 'offline') {
        const dbError: ConnectionError = {
          type: 'database-offline',
          details: {
            message: 'Database is offline', // Fallback message, UI will translate
            attemptedUrl: config.apiUrl,
          },
        }
        setError(dbError)
        isCheckingRef.current = false
        setIsChecking(false)
        return
      }

      // If we got here, connection is good
      setError(null)
      // T3 hibernate signal: banner only, children still render.
      setDraining(config.draining ?? false)
      isCheckingRef.current = false
      setIsChecking(false)
    } catch (err) {
      // API is unreachable
      const errorMessage = err instanceof Error ? err.message : 'Unknown error'
      const attemptedUrl =
        typeof window !== 'undefined'
          ? `${window.location.origin}/api/config`
          : undefined

      const apiError: ConnectionError = {
        type: 'api-unreachable',
        details: {
          message: 'Unable to connect to API', // Fallback message
          technicalMessage: errorMessage,
          stack: err instanceof Error ? err.stack : undefined,
          attemptedUrl,
        },
      }
      
      setError(apiError)
      isCheckingRef.current = false
      setIsChecking(false)
    }
  }, []) // Empty dependency array - stable callback

  // Check connection on mount
  useEffect(() => {
    checkConnection()
  }, [checkConnection])

  // Add keyboard shortcut for retry (R key)
  useEffect(() => {
    const handleKeyPress = (e: KeyboardEvent) => {
      if (error && (e.key === 'r' || e.key === 'R')) {
        e.preventDefault()
        checkConnection()
      }
    }

    window.addEventListener('keydown', handleKeyPress)
    return () => window.removeEventListener('keydown', handleKeyPress)
  }, [error, checkConnection])

  // Show overlay if there's an error
  if (error) {
    return <ConnectionErrorOverlay error={error} onRetry={checkConnection} />
  }

  // Show nothing while checking (prevents flash of content)
  if (isChecking) {
    return null
  }

  // Render children if connection is good; draining shows a banner only.
  const handleWake = async () => {
    setWaking(true)
    setWakeError(null)
    try {
      const { governorApi } = await import('@/lib/api/governor')
      await governorApi.wake()
      await checkConnection()
    } catch (err) {
      setWakeError(err instanceof Error ? err.message : t('training.wakeFailed'))
    } finally {
      setWaking(false)
    }
  }

  return (
    <>
      {draining && (
        <div
          role="status"
          className="flex w-full items-center justify-center gap-3 bg-amber-100 px-4 py-2 text-center text-sm text-amber-900 dark:bg-amber-900 dark:text-amber-100"
        >
          <span>{t('training.hibernating')}</span>
          <button
            type="button"
            onClick={handleWake}
            disabled={waking}
            className="rounded border border-amber-700 px-2 py-0.5 text-xs font-medium disabled:opacity-50 dark:border-amber-200"
          >
            {waking ? t('training.waking') : t('training.wake')}
          </button>
          {wakeError && <span className="text-xs">{wakeError}</span>}
        </div>
      )}
      {children}
    </>
  )
}
