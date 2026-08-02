import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // ECharts is isolated in one lazy shared chunk; 650 kB keeps the warning
    // focused on accidental growth beyond the reviewed charting baseline.
    chunkSizeWarningLimit: 650,
  },
})
