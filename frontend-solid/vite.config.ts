import { defineConfig } from 'vite'
import solid from 'vite-plugin-solid'
import path from 'path'

export default defineConfig({
  plugins: [solid()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:5055',
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 8502,
    proxy: {
      '/api': {
        target: 'http://localhost:5055',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
    minify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          // Vendor chunk: node_modules
          if (id.includes('node_modules')) {
            if (id.includes('solid-js') || id.includes('@solidjs')) {
              return 'vendor-solid'
            }
            if (id.includes('axios') || id.includes('i18next')) {
              return 'vendor-core'
            }
            if (id.includes('lucide-solid')) {
              return 'vendor-icons'
            }
            return 'vendor'
          }
        },
        chunkFileNames: (chunkInfo) => {
          // Keep entry as index-*.js for the app code
          if (chunkInfo.name === 'index') {
            return 'assets/index-[name]-[hash].js'
          }
          return 'assets/[name]-[hash].js'
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
