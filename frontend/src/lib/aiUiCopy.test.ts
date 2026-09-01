import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const files = [
  'pages/StockAnalysis.tsx',
  'pages/Review.tsx',
  'pages/Agent.tsx',
  'components/stock-analysis/StockAnalysisDialog.tsx',
  'components/financials/AiAnalysisDialog.tsx',
  'components/screener/StrategyBuilderDialog.tsx',
  'components/agent/ShortPoolPanel.tsx',
  'components/research/TSuitabilityPanel.tsx',
]

const forbidden = ['买入区间', '操作建议', '可直接指导次日仓位', '推荐买入']

for (const rel of files) {
  const text = readFileSync(join(root, rel), 'utf8')
  for (const phrase of forbidden) {
    assert(!text.includes(phrase), `${rel} 不得包含禁词「${phrase}」`)
  }
}

const shortPoolPanel = readFileSync(join(root, 'components/agent/ShortPoolPanel.tsx'), 'utf8')
const evidenceApi = readFileSync(join(root, 'features/research/api/evidence.ts'), 'utf8')
assert(shortPoolPanel.includes('confirmTSuitabilityHypothesis'), '做T确认必须调用服务端重算门禁入口')
assert(evidenceApi.includes('/api/research/t-suitability/hypotheses'), '做T确认必须打到专用服务端门禁路径')
assert(!shortPoolPanel.includes('researchCreateHypothesis'), '做T确认不得绕过专用服务端门禁')
assert(!shortPoolPanel.includes('createHypothesis('), '做T确认不得走通用假设创建入口')

console.log('aiUiCopy.test.ts ok')
