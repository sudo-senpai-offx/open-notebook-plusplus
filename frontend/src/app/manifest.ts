import type { MetadataRoute } from 'next'

/** PWA installability (vehicle-1 companion client): name, standalone
 * display, maskable-capable icons, theme colors. Offline asset caching
 * lives in public/sw.js; the export-migration variant is vehicle-2-only. */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'Open Notebook PlusPlus',
    short_name: 'Notebook++',
    description: 'Privacy-focused research, study, and review — offline-first',
    start_url: '/',
    display: 'standalone',
    background_color: '#ffffff',
    theme_color: '#0f766e',
    icons: [
      {
        src: '/icons/icon-192.png',
        sizes: '192x192',
        type: 'image/png',
      },
      {
        src: '/icons/icon-512.png',
        sizes: '512x512',
        type: 'image/png',
      },
    ],
  }
}
