import { create } from "zustand"
import { getStorageEstimate, requestPersistentStorage } from "@/lib/offline-storage"

interface OfflineState {
  isOnline: boolean
  isLoading: boolean
  cacheUsage: number
  cacheQuota: number
  cacheUsagePercentage: number
  isPersistent: boolean | null

  setOnline: (online: boolean) => void
  setLoading: (loading: boolean) => void
  refreshStorage: () => Promise<void>
  requestPersistent: () => Promise<void>
  clearCache: () => Promise<void>
}

export const useOfflineStore = create<OfflineState>((set) => ({
  isOnline: typeof navigator !== "undefined" ? navigator.onLine : true,
  isLoading: false,
  cacheUsage: 0,
  cacheQuota: 0,
  cacheUsagePercentage: 0,
  isPersistent: null,

  setOnline: (online: boolean) => set({ isOnline: online }),

  setLoading: (loading: boolean) => set({ isLoading: loading }),

  refreshStorage: async () => {
    set({ isLoading: true })
    try {
      const estimate = await getStorageEstimate()
      set({
        cacheUsage: estimate.usage,
        cacheQuota: estimate.quota,
        cacheUsagePercentage: estimate.usagePercentage,
      })
    } finally {
      set({ isLoading: false })
    }
  },

  requestPersistent: async () => {
    const persisted = await requestPersistentStorage()
    set({ isPersistent: persisted })
  },

  clearCache: async () => {
    set({ isLoading: true })
    try {
      // Clear IndexedDB via offline-storage
      const { clearAllOfflineData } = await import("@/lib/offline-storage")
      await clearAllOfflineData()

      // Clear Cache API
      if (typeof caches !== "undefined") {
        const cacheNames = await caches.keys()
        await Promise.all(
          cacheNames
            .filter((name) => name.startsWith("onpp-"))
            .map((name) => caches.delete(name)),
        )
      }

      set({
        cacheUsage: 0,
        cacheQuota: 0,
        cacheUsagePercentage: 0,
      })
    } finally {
      set({ isLoading: false })
    }
  },
}))
