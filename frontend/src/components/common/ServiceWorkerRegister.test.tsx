import { render, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import manifest from '@/app/manifest'
import { ServiceWorkerRegister } from './ServiceWorkerRegister'

describe('PWA installability', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    // jsdom has no serviceWorker; each test defines its own double.
    delete (window.navigator as unknown as Record<string, unknown>).serviceWorker
  })

  it('serves a standalone manifest with sized icons', () => {
    const m = manifest()
    expect(m.display).toBe('standalone')
    expect(m.start_url).toBe('/')
    const sizes = (m.icons ?? []).map(i => i.sizes)
    expect(sizes).toContain('192x192')
    expect(sizes).toContain('512x512')
    expect(m.theme_color).toBeDefined()
  })

  it('registers /sw.js when supported', async () => {
    const register = vi.fn().mockResolvedValue({} as ServiceWorkerRegistration)
    Object.defineProperty(window.navigator, 'serviceWorker', {
      value: { register },
      configurable: true,
    })
    render(<ServiceWorkerRegister />)
    await waitFor(() => expect(register).toHaveBeenCalledWith('/sw.js'))
  })

  it('renders nothing without service worker support', () => {
    Object.defineProperty(window.navigator, 'serviceWorker', {
      value: undefined,
      configurable: true,
    })
    const { container } = render(<ServiceWorkerRegister />)
    expect(container.firstChild).toBeNull()
  })
})
