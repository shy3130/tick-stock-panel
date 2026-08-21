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
]

const forbidden = ['买入区间', '操作建议', '可直接指导次日仓位']

for (const rel of files) {
  const text = readFileSync(join(root, rel), 'utf8')
  for (const phrase of forbidden) {
    assert(!text.includes(phrase), `${rel} 不得包含禁词「${phrase}」`)
  }
}

console.log('aiUiCopy.test.ts ok')
