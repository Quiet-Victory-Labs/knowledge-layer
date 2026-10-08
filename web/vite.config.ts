import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            // Keep each runtime's initialization cycle inside one chunk.
            { name: 'ui-vendor', test: /\/node_modules\/(?:react|react-dom|scheduler|@mui|@emotion)\// },
            { name: 'graph-vendor', test: /\/node_modules\/cytoscape\// },
            { name: 'graph-layout-vendor', test: /\/node_modules\/(?:cytoscape-fcose|cose-base|layout-base)\// },
          ],
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        // Keep the browser's host for managed-origin checks and setup URLs.
        changeOrigin: false,
      },
      '/auth': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: false,
      },
      '/agent': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: false,
      },
    },
  },
});
