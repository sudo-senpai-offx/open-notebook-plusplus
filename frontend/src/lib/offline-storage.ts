/**
 * OFFLINE STORAGE — IndexedDB layer for ON++ Study OS
 *
 * Stores structured data locally for offline-first operation:
 * - notebooks (courses/subjects)
 * - sources (materials/resources/skills)
 * - assessments (quiz/exam results for spiderweb metrics)
 * - timetable (scheduled blocks)
 * - reflections (post-completion minimal reviews)
 * - settings (merged settings page state)
 * - cache metadata (size, last sync)
 *
 * Strategy:
 * - Cache API: app shell (HTML/CSS/JS/images) — handled by service worker
 * - IndexedDB: structured records and blobs — this module
 * - localStorage: tiny flags only (theme, language)
 *
 * All writes are transactional and async. Reads return promises.
 * When offline, reads come from IndexedDB; writes are queued and synced
 * when the connection returns.
 */

const DB_NAME = "onpp-offline"
const DB_VERSION = 1

// Object store names
export const STORES = {
  NOTEBOOKS: "notebooks",
  SOURCES: "sources",
  ASSESSMENTS: "assessments",
  TIMETABLE: "timetable",
  REFLECTIONS: "reflections",
  SETTINGS: "settings",
  CACHE_META: "cache-meta",
} as const

type StoreName = (typeof STORES)[keyof typeof STORES]

// IndexedDB instance
let dbPromise: Promise<IDBDatabase> | null = null

function openDB(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise

  dbPromise = new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB not available in this environment"))
      return
    }

    const request = indexedDB.open(DB_NAME, DB_VERSION)

    request.onerror = () => reject(request.error)
    request.onsuccess = () => resolve(request.result)

    request.onupgradeneeded = (event) => {
      const db = (event.target as IDBOpenDBRequest).result

      // Notebooks = courses/subjects
      if (!db.objectStoreNames.contains(STORES.NOTEBOOKS)) {
        const notebooks = db.createObjectStore(STORES.NOTEBOOKS, { keyPath: "id" })
        notebooks.createIndex("field", "field", { unique: false })
        notebooks.createIndex("curated", "curated", { unique: false })
        notebooks.createIndex("updatedAt", "updatedAt", { unique: false })
      }

      // Sources = materials/resources/skills inside notebooks
      if (!db.objectStoreNames.contains(STORES.SOURCES)) {
        const sources = db.createObjectStore(STORES.SOURCES, { keyPath: "id" })
        sources.createIndex("notebookId", "notebookId", { unique: false })
        sources.createIndex("field", "field", { unique: false })
        sources.createIndex("moduleId", "moduleId", { unique: false })
        sources.createIndex("updatedAt", "updatedAt", { unique: false })
      }

      // Assessments = quiz/exam results (spiderweb metrics input)
      if (!db.objectStoreNames.contains(STORES.ASSESSMENTS)) {
        const assessments = db.createObjectStore(STORES.ASSESSMENTS, { keyPath: "id" })
        assessments.createIndex("notebookId", "notebookId", { unique: false })
        assessments.createIndex("field", "field", { unique: false })
        assessments.createIndex("type", "type", { unique: false }) // quiz | exam | mock
        assessments.createIndex("createdAt", "createdAt", { unique: false })
      }

      // Timetable = scheduled blocks
      if (!db.objectStoreNames.contains(STORES.TIMETABLE)) {
        const timetable = db.createObjectStore(STORES.TIMETABLE, { keyPath: "id" })
        timetable.createIndex("date", "date", { unique: false })
        timetable.createIndex("notebookId", "notebookId", { unique: false })
      }

      // Reflections = post-completion minimal reviews
      if (!db.objectStoreNames.contains(STORES.REFLECTIONS)) {
        const reflections = db.createObjectStore(STORES.REFLECTIONS, { keyPath: "id" })
        reflections.createIndex("notebookId", "notebookId", { unique: false })
        reflections.createIndex("createdAt", "createdAt", { unique: false })
      }

      // Settings = merged settings page state
      if (!db.objectStoreNames.contains(STORES.SETTINGS)) {
        db.createObjectStore(STORES.SETTINGS, { keyPath: "key" })
      }

      // Cache metadata
      if (!db.objectStoreNames.contains(STORES.CACHE_META)) {
        db.createObjectStore(STORES.CACHE_META, { keyPath: "key" })
      }
    }
  })

  return dbPromise
}

/** Get a transaction for a store in readwrite or readonly mode */
async function tx<T>(
  storeNames: StoreName | StoreName[],
  mode: IDBTransactionMode,
  fn: (tx: IDBTransaction) => T,
): Promise<T> {
  const db = await openDB()
  const stores = Array.isArray(storeNames) ? storeNames : [storeNames]
  const transaction = db.transaction(stores, mode)
  return new Promise((resolve, reject) => {
    transaction.onerror = () => reject(transaction.error)
    transaction.oncomplete = () => resolve(fn(transaction))
  })
}

/** Get all records from a store */
export async function dbGetAll<T>(storeName: StoreName): Promise<T[]> {
  return tx(storeName, "readonly", (t) => {
    const store = t.objectStore(storeName)
    const request = store.getAll()
    return new Promise<T[]>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result as T[])
      request.onerror = () => reject(request.error)
    })
  })
}

/** Get a single record by key */
export async function dbGet<T>(storeName: StoreName, key: string): Promise<T | null> {
  return tx(storeName, "readonly", (t) => {
    const store = t.objectStore(storeName)
    const request = store.get(key)
    return new Promise<T | null>((resolve, reject) => {
      request.onsuccess = () => resolve((request.result as T) ?? null)
      request.onerror = () => reject(request.error)
    })
  })
}

/** Put (upsert) a record */
export async function dbPut<T>(
  storeName: StoreName,
  record: T,
): Promise<void> {
  return tx(storeName, "readwrite", (t) => {
    const store = t.objectStore(storeName)
    const request = store.put(record)
    return new Promise<void>((resolve, reject) => {
      request.onsuccess = () => resolve()
      request.onerror = () => reject(request.error)
    })
  })
}

/** Delete a record by key */
export async function dbDel(storeName: StoreName, key: IDBValidKey): Promise<void> {
  return tx(storeName, "readwrite", (t) => {
    const store = t.objectStore(storeName)
    const request = store.delete(key)
    return new Promise<void>((resolve, reject) => {
      request.onsuccess = () => resolve()
      request.onerror = () => reject(request.error)
    })
  })
}

/** Clear all records from a store */
export async function dbClear(storeName: StoreName): Promise<void> {
  return tx(storeName, "readwrite", (t) => {
    const store = t.objectStore(storeName)
    const request = store.clear()
    return new Promise<void>((resolve, reject) => {
      request.onsuccess = () => resolve()
      request.onerror = () => reject(request.error)
    })
  })
}

/** Get records by index */
export async function dbGetByIndex<T>(
  storeName: StoreName,
  indexName: string,
  value: unknown,
): Promise<T[]> {
  return tx(storeName, "readonly", (t) => {
    const store = t.objectStore(storeName)
    const index = store.index(indexName)
    const request = index.getAll(value as IDBValidKey | IDBKeyRange)
    return new Promise<T[]>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result as T[])
      request.onerror = () => reject(request.error)
    })
  })
}

/** Count records in a store */
export async function dbCount(storeName: StoreName): Promise<number> {
  return tx(storeName, "readonly", (t) => {
    const store = t.objectStore(storeName)
    const request = store.count()
    return new Promise<number>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result)
      request.onerror = () => reject(request.error)
    })
  })
}

/** Get estimated storage usage */
export async function getStorageEstimate(): Promise<{
  usage: number
  quota: number
  usagePercentage: number
}> {
  if (typeof navigator.storage === "undefined" || !navigator.storage.estimate) {
    return { usage: 0, quota: 0, usagePercentage: 0 }
  }
  const estimate = await navigator.storage.estimate()
  return {
    usage: estimate.usage ?? 0,
    quota: estimate.quota ?? 0,
    usagePercentage:
      estimate.quota && estimate.usage
        ? Math.round((estimate.usage / estimate.quota) * 100)
        : 0,
  }
}

/** Request persistent storage (prevents eviction under storage pressure) */
export async function requestPersistentStorage(): Promise<boolean> {
  if (
    typeof navigator.storage === "undefined" ||
    !navigator.storage.persist
  ) {
    return false
  }
  return navigator.storage.persist()
}

/** Clear ALL offline data (nuclear option for Settings "Clear Cache") */
export async function clearAllOfflineData(): Promise<void> {
  const db = await openDB()
  const storeNames: StoreName[] = Array.from(db.objectStoreNames) as StoreName[]
  await tx(
    storeNames,
    "readwrite",
    () => {
      // clearing each store
    },
  )
  for (const storeName of storeNames) {
    await dbClear(storeName)
  }
}
