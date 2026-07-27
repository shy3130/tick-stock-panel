import type { PortfolioTrade } from './api'

type CsvValue = string | number | null | undefined

interface TradeColumn {
  header: string
  value: (trade: PortfolioTrade) => CsvValue
}

const TRADE_COLUMNS: TradeColumn[] = [
  { header: '交易ID', value: trade => trade.id },
  { header: '交易日期', value: trade => trade.trade_date },
  { header: '方向', value: trade => trade.side === 'buy' ? '买入' : '卖出' },
  { header: '标的代码', value: trade => trade.symbol },
  { header: '标的名称', value: trade => trade.name },
  { header: '数量', value: trade => trade.quantity },
  { header: '成交价', value: trade => trade.price },
  { header: '手续费', value: trade => trade.fees },
  { header: '成交金额', value: trade => trade.quantity * trade.price },
  { header: '备注', value: trade => trade.note },
  { header: '创建时间', value: trade => trade.created_at },
  { header: '更新时间', value: trade => trade.updated_at },
]

const FORMULA_PREFIX = /^[=+\-@]/

function encodeCsvCell(value: CsvValue): string {
  if (value == null) return ''
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : ''

  const text = FORMULA_PREFIX.test(value.trimStart()) ? `'${value}` : value
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function localDatePart(date: Date): string {
  const year = String(date.getFullYear()).padStart(4, '0')
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function buildPortfolioTradesCsv(trades: PortfolioTrade[]): string {
  const header = TRADE_COLUMNS.map(column => encodeCsvCell(column.header)).join(',')
  const rows = trades.map(trade => (
    TRADE_COLUMNS.map(column => encodeCsvCell(column.value(trade))).join(',')
  ))
  return `\uFEFF${[header, ...rows].join('\r\n')}`
}

export function buildPortfolioTradeExportFilename(exportedAt = new Date()): string {
  return `持仓交易流水_${localDatePart(exportedAt)}.csv`
}
