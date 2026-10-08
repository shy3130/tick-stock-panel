// 分钟尾部续画纯函数的行为矩阵测试。
// 核心回归: 行情日期非当日的资产类型 (ETF 未开实时拉取, enriched close 是旧日K
// 回退值) 不得被续画 — 旧价拼到当日分钟序列尾部会画出错价长刺 (issue 场景)。
import { describe, expect, it } from 'vitest'
import { extendMinuteTail, type TailExtendLiveRow } from './minuteTailExtend'
import type { MinuteKlineRow } from '@/lib/api'

const NOW = new Date(2026, 9, 8, 10, 30) // 2026-10-08 10:30 (连续竞价时段)
const TODAY = '2026-10-08'

function bars(...datetimes: string[]): MinuteKlineRow[] {
  return datetimes.map((dt, i) => ({
    datetime: dt, open: 10 + i, high: 10.5 + i, low: 9.5 + i, close: 10 + i,
    volume: 100, amount: 1000,
  }))
}

const BASE = {
  '600519.SH': bars(`${TODAY}T10:28:00`, `${TODAY}T10:29:00`),
  '588200.SH': bars(`${TODAY}T10:28:00`, `${TODAY}T10:29:00`),
}

const LIVE: TailExtendLiveRow[] = [
  { symbol: '600519.SH', close: 1900.0, asset_type: 'stock' },
  { symbol: '588200.SH', close: 1.105, asset_type: 'etf' }, // 旧日K回退价, 与当日 10.x 序列严重偏离
]

const DATES_FRESH_STOCK_STALE_ETF = { stock: TODAY, etf: '2026-09-29', index: null }

describe('extendMinuteTail 新鲜度守卫', () => {
  it('当日行情的标的正常续画: 追加当前分钟的平K', () => {
    const out = extendMinuteTail(BASE, LIVE, DATES_FRESH_STOCK_STALE_ETF, NOW)
    const stock = out['600519.SH']
    expect(stock).toHaveLength(3)
    expect(stock[2]).toMatchObject({
      datetime: `${TODAY}T10:30:00`, open: 1900, high: 1900, low: 1900, close: 1900,
    })
  })

  it('行情日期非当日的标的 (ETF 旧日K回退) 原样返回 — 核心回归', () => {
    const out = extendMinuteTail(BASE, LIVE, DATES_FRESH_STOCK_STALE_ETF, NOW)
    expect(out['588200.SH']).toBe(BASE['588200.SH'])
  })

  it('dates 缺失 (旧后端) 整体不续画 (fail-closed)', () => {
    const out = extendMinuteTail(BASE, LIVE, undefined, NOW)
    expect(out).toBe(BASE)
    expect(extendMinuteTail(BASE, LIVE, null, NOW)).toBe(BASE)
  })

  it('ETF 行情日期为当日 (开了实时拉取) 时恢复续画 — 守卫按资产类型独立判定', () => {
    const out = extendMinuteTail(BASE, LIVE,
      { stock: TODAY, etf: TODAY, index: null }, NOW)
    expect(out['588200.SH']).toHaveLength(3)
    expect(out['588200.SH'][2].close).toBe(1.105)
  })

  it('价格为 0/缺失的标的跳过续画 (close>0 硬校验)', () => {
    const live: TailExtendLiveRow[] = [
      { symbol: '600519.SH', asset_type: 'stock' },          // rt_price/close 都缺 → 跳过
      { symbol: '588200.SH', close: 0, asset_type: 'etf' },  // 0 值 → 跳过
    ]
    const dates = { stock: TODAY, etf: TODAY, index: null }
    const out = extendMinuteTail(BASE, live, dates, NOW)
    expect(out['600519.SH']).toBe(BASE['600519.SH'])
    expect(out['588200.SH']).toBe(BASE['588200.SH'])
  })

  it('当前分钟已有K时走修补路径: 只更新 close 并合成 high/low', () => {
    const base = { '600519.SH': bars(`${TODAY}T10:29:00`, `${TODAY}T10:30:00`) }
    const out = extendMinuteTail(base, LIVE, DATES_FRESH_STOCK_STALE_ETF, NOW)
    const last = out['600519.SH'][1]
    expect(out['600519.SH']).toHaveLength(2)
    expect(last.close).toBe(1900)
    expect(last.high).toBe(1900)  // max(10.5+1, 1900)
    expect(last.low).toBe(10.5)   // min(9.5+1, 1900)
  })

  it('非连续竞价时段 (午休/收盘后) 不续画', () => {
    const lunch = new Date(2026, 9, 8, 12, 0)
    expect(extendMinuteTail(BASE, LIVE, DATES_FRESH_STOCK_STALE_ETF, lunch)).toBe(BASE)
    const afterClose = new Date(2026, 9, 8, 15, 30)
    expect(extendMinuteTail(BASE, LIVE, DATES_FRESH_STOCK_STALE_ETF, afterClose)).toBe(BASE)
  })

  it('liveRows 为空时不续画', () => {
    expect(extendMinuteTail(BASE, undefined, DATES_FRESH_STOCK_STALE_ETF, NOW)).toBe(BASE)
    expect(extendMinuteTail(BASE, [], DATES_FRESH_STOCK_STALE_ETF, NOW)).toBe(BASE)
  })
})
