import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const backend = 'http://127.0.0.1:4290';
const frontend = 'http://127.0.0.1:4291';

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 30_000,
  reporter: 'list',
  use: { baseURL: frontend, trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { baseURL: backend, viewport: { width: 1280, height: 720 } } },
    { name: 'mobile', use: { baseURL: backend, viewport: { width: 390, height: 844 } } },
    { name: 'dev-desktop', use: { viewport: { width: 1280, height: 720 } } },
  ],
  webServer: [
    {
      command: 'uv run --no-sync uvicorn knowledge_layer.platform:app --host 127.0.0.1 --port 4290 --no-access-log',
      cwd: root,
      url: backend + '/api/ready',
      env: {
        KNOWLEDGE_AUTH_MODE: 'demo',
        DATABASE_URL: 'sqlite:///:memory:',
        KNOWLEDGE_FIXTURE_ROOT: root + '/data/connectable',
        KNOWLEDGE_WEB_DIST: root + '/web/dist',
        KNOWLEDGE_AGENT_AUTONOMY: '0',
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: 'npm run dev -- --port 4291 --strictPort',
      url: frontend,
      env: { VITE_API_TARGET: backend },
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
