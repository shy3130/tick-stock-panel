import test from 'node:test'
import assert from 'node:assert/strict'

import type { PortfolioTrade } from './api.ts'
import { buildPortfolioTradeExportFilename, buildPortfolioTradesCsv } from './portfolioTradeCsv.ts'

function makeTrade(overrides: Partial<PortfolioTrade> = {}): PortfolioTrade {
  return {
    id: 'trade-001',
    symbol: '000001.SZ',
    name: '平安银行',
    side: 'buy',
    quantity: 1200,
    price: 10.25,
    fees: 5.5,
    trade_date: '2026-07-25',
    note: '首次建仓',
    created_at: '2026-07-25T09:31:00+08:00',
    updated_at: '2026-07-25T09:31:00+08:00',
    ...overrides,
  }
}

test('builds an Excel-compatible CSV with all portfolio trade fields', () => {
  const csv = buildPortfolioTradesCsv([
    makeTrade(),
    makeTrade({ id: 'trade-002', side: 'sell', quantity: 200, price: 10.8 }),
  ])

  assert.ok(csv.startsWith('\uFEFF交易ID,交易日期,方向,标的代码,标的名称,数量,成交价,手续费,成交金额,备注,创建时间,更新时间'))
  assert.ok(csv.includes('\r\ntrade-001,2026-07-25,买入,000001.SZ,平安银行,1200,10.25,5.5,12300,首次建仓'))
  assert.ok(csv.includes('\r\ntrade-002,2026-07-25,卖出,000001.SZ,平安银行,200,10.8,5.5,2160,首次建仓'))
  assert.equal(csv.split('\r\n').length, 3)
})

test('escapes CSV text containing commas, quotes and newlines', () => {
  const csv = buildPortfolioTradesCsv([
    makeTrade({ name: '测试,"名称"', note: '第一行\n第二行' }),
  ])

  assert.ok(csv.includes('"测试,""名称"""'))
  assert.ok(csv.includes('"第一行\n第二行"'))
})

test('neutralizes spreadsheet formulas without changing negative numbers', () => {
  const csv = buildPortfolioTradesCsv([
    makeTrade({ id: '+CMD', symbol: '=1+1', name: '  @SUM(A1:A2)', note: '-危险公式', fees: -2.5 }),
  ])

  assert.ok(csv.includes("\r\n'+CMD,"))
  assert.ok(csv.includes(",'=1+1,'  @SUM(A1:A2),"))
  assert.ok(csv.includes(",'-危险公式,"))
  assert.ok(csv.includes(',-2.5,12300,'))
})

test('uses empty cells for non-finite numeric values', () => {
  const csv = buildPortfolioTradesCsv([
    makeTrade({ quantity: Number.NaN, price: Number.POSITIVE_INFINITY }),
  ])
  const row = csv.split('\r\n')[1]

  assert.ok(row.includes(',平安银行,,,5.5,,首次建仓,'))
})

test('builds a stable date-based export filename', () => {
  const filename = buildPortfolioTradeExportFilename(new Date(2026, 6, 27, 23, 59))

  assert.equal(filename, '持仓交易流水_2026-07-27.csv')
})
