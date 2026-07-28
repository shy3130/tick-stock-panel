import { mkdir, rm } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const artifactRoot = path.resolve(frontendDir, '..', 'output', 'playwright')
const dataDir = path.join(artifactRoot, 'e2e-data')

if (path.basename(artifactRoot) !== 'playwright' || path.basename(dataDir) !== 'e2e-data') {
  throw new Error(`Refusing to clean unexpected E2E path: ${dataDir}`)
}

await rm(dataDir, { recursive: true, force: true })
await mkdir(dataDir, { recursive: true })
