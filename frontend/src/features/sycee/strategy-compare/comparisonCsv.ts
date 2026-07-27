import type { ComparisonMetricRow, ComparisonSettings } from './comparison'

type CsvValue = string | number | null

const FORMULA_PREFIX = /^[=+\-@\t\r]/

function csvCell(value: CsvValue): string {
  if (value == null) return ''
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : ''
  const safe = FORMULA_PREFIX.test(value) ? `'${value}` : value
  return /[",\r\n]/.test(safe) ? `"${safe.replaceAll('"', '""')}"` : safe
}

export function buildComparisonCsv(
  rows: ComparisonMetricRow[],
  settings: ComparisonSettings,
): string {
  const headers = [
    '策略ID', '策略名称', '开始日期', '结束日期', '股票池', '初始资金',
    '总收益率', '年化收益率', '夏普比率', '最大回撤', '胜率', '交易次数', '耗时毫秒',
  ]
  const body = rows.map(row => [
    row.strategyId,
    row.strategyName,
    settings.start,
    settings.end,
    settings.symbols.join('|'),
    settings.initialCapital,
    row.totalReturn,
    row.annualReturn,
    row.sharpe,
    row.maxDrawdown,
    row.winRate,
    row.tradeCount,
    row.elapsedMs,
  ].map(csvCell).join(','))
  return `\uFEFF${[headers.map(csvCell).join(','), ...body].join('\r\n')}\r\n`
}

export function downloadComparisonCsv(
  rows: ComparisonMetricRow[],
  settings: ComparisonSettings,
): void {
  const blob = new Blob([buildComparisonCsv(rows, settings)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `strategy-comparison-${settings.start}-${settings.end}.csv`
  link.click()
  URL.revokeObjectURL(url)
}
