import type { StrategyBacktestResult, StrategyBacktestTrade } from '@/lib/api'

type CsvValue = string | number | boolean | null | undefined

interface TradeColumn {
  header: string
  value: (trade: StrategyBacktestTrade, result: StrategyBacktestResult) => CsvValue
}

const TRADE_COLUMNS: TradeColumn[] = [
  { header: '回测ID', value: (_trade, result) => result.run_id },
  { header: '策略ID', value: (_trade, result) => result.strategy_info?.id },
  { header: '策略名称', value: (_trade, result) => result.strategy_info?.name },
  { header: '标的代码', value: trade => trade.symbol },
  { header: '标的名称', value: trade => trade.name },
  { header: '入场信号日期', value: trade => trade.entry_signal_date },
  { header: '买入日期', value: trade => trade.entry_date },
  { header: '买入价格', value: trade => trade.entry_price },
  { header: '入场信号', value: trade => trade.entry_signal_id },
  { header: '入场评分', value: trade => trade.entry_score },
  { header: '出场信号日期', value: trade => trade.exit_signal_date },
  { header: '卖出日期', value: trade => trade.exit_date },
  { header: '卖出价格', value: trade => trade.exit_price },
  { header: '出场信号', value: trade => trade.exit_signal_id },
  { header: '退出原因', value: trade => trade.exit_reason },
  { header: '持仓天数', value: trade => trade.duration },
  { header: '卖出受阻天数', value: trade => trade.blocked_exit_days },
  { header: '股数', value: trade => trade.shares },
  { header: '手数', value: trade => trade.lots },
  { header: '仓位比例', value: trade => trade.position_pct },
  { header: '买入金额', value: trade => trade.entry_value },
  { header: '卖出金额', value: trade => trade.exit_value },
  { header: '盈亏金额', value: trade => trade.pnl_amount },
  { header: '收益率', value: trade => trade.pnl_pct },
]

const FORMULA_PREFIX = /^[=+\-@\t\r]/

function encodeCsvCell(value: CsvValue): string {
  if (value == null) return ''
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : ''
  if (typeof value === 'boolean') return value ? 'true' : 'false'

  const text = FORMULA_PREFIX.test(value.trimStart()) ? `'${value}` : value
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function datePart(value: unknown): string {
  return typeof value === 'string' ? value.slice(0, 10) : ''
}

function safeFilenamePart(value: unknown): string {
  if (typeof value !== 'string') return ''
  return value
    .trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]+/g, '_')
    .replace(/\s+/g, '_')
    .replace(/^\.+|\.+$/g, '')
    .slice(0, 48)
}

export function buildBacktestTradesCsv(result: StrategyBacktestResult): string {
  const header = TRADE_COLUMNS.map(column => encodeCsvCell(column.header)).join(',')
  const rows = result.trades.map(trade => (
    TRADE_COLUMNS.map(column => encodeCsvCell(column.value(trade, result))).join(',')
  ))
  return `\uFEFF${[header, ...rows].join('\r\n')}`
}

export function buildBacktestTradeExportFilename(result: StrategyBacktestResult): string {
  const strategy = safeFilenamePart(result.strategy_info?.name)
    || safeFilenamePart(result.strategy_info?.id)
    || 'strategy'
  const start = datePart(result.config?.start) || datePart(result.equity_curve[0]?.date)
  const end = datePart(result.config?.end) || datePart(result.equity_curve.at(-1)?.date)
  const range = start && end ? `_${start}_${end}` : ''
  return `回测交易明细_${strategy}${range}.csv`
}
