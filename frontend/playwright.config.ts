import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendDir = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(frontendDir, '..')
const dataDir = path.join(repoRoot, 'output', 'playwright', 'e2e-data')
const baseURL = 'http://127.0.0.1:43118'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  outputDir: path.join(repoRoot, 'output', 'playwright', 'test-results'),
  reporter: [
    ['list'],
    ['html', { outputFolder: path.join(repoRoot, 'output', 'playwright', 'report'), open: 'never' }],
  ],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: [
      'pnpm build',
      'node e2e/prepare.mjs',
      'cd ../backend',
      'uv run --frozen --extra dev uvicorn app.main:app --lifespan off --host 127.0.0.1 --port 43118',
    ].join(' && '),
    cwd: frontendDir,
    env: {
      DATA_DIR: dataDir,
      INVITE_CODES: 'E2E-ALICE,E2E-BOB',
      BACKTEST_MATRIX_DISK_CACHE_ENABLED: 'false',
    },
    url: `${baseURL}/health`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
