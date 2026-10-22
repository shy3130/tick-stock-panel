// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { mergeColumns } from './list-columns'
import { getSortValue } from './stock-table'
import { BUILTIN_COLUMNS, COLUMN_GROUPS } from './watchlist-columns'

// #328: 自选页新增 今开/最高/最低/昨收/成交量/涨停价/跌停价 七列。
// 关键兼容契约: 老用户的持久化配置不含这些列 id, mergeColumns 必须把它们
// 补进结果且保持默认隐藏 (visible:false), 已有列的用户显隐不受影响。
describe('watchlist columns: 新增内置列向后兼容', () => {
  const NEW_KEYS = ['open', 'high', 'low', 'prev_close', 'volume', 'limit_up_price', 'limit_down_price']

  it('新增列已注册且默认隐藏, 并归入价格/成交分组', () => {
    for (const key of NEW_KEYS) {
      const col = BUILTIN_COLUMNS.find(c => c.source.type === 'builtin' && c.source.key === key)
      expect(col, `内置列 ${key} 应存在`).toBeTruthy()
      expect(col!.visible).toBe(false)
    }
    const groupKeys = new Set(COLUMN_GROUPS.flatMap(g => g.keys))
    for (const key of NEW_KEYS) {
      expect(groupKeys.has(key), `列 ${key} 应出现在 COLUMN_GROUPS 中`).toBe(true)
    }
  })

  it('老用户配置 (不含新列 id) 合并后自动补齐新列且默认隐藏', () => {
    // 模拟新列上线前保存的配置: 只有部分老列
    const saved = BUILTIN_COLUMNS
      .filter(c => ['builtin:symbol', 'builtin:price', 'builtin:pct', 'builtin:turnover'].includes(c.id))
      .map(c => ({ ...c }))

    const merged = mergeColumns(saved, BUILTIN_COLUMNS)
    const byId = new Map(merged.map(c => [c.id, c]))

    for (const key of NEW_KEYS) {
      const col = byId.get(`builtin:${key}`)
      expect(col, `合并后应包含新列 builtin:${key}`).toBeTruthy()
      expect(col!.visible).toBe(false)
    }
    // 用户可见性保留: 老列的 visible 不被新默认值覆盖
    expect(byId.get('builtin:price')!.visible).toBe(true)
    expect(byId.get('builtin:symbol')!.visible).toBe(true)
    // 用户顺序在前, 新列追加在后
    expect(merged[0].id).toBe('builtin:symbol')
    expect(merged.slice(0, 4).map(c => c.id)).toEqual(
      ['builtin:symbol', 'builtin:price', 'builtin:pct', 'builtin:turnover'],
    )
  })

  it('用户开启过的新列 (版本间来回) 显隐被保留', () => {
    const saved = BUILTIN_COLUMNS
      .filter(c => c.id === 'builtin:symbol' || c.id === 'builtin:volume')
      .map(c => ({ ...c, visible: c.id === 'builtin:volume' }))

    const merged = mergeColumns(saved, BUILTIN_COLUMNS)
    const byId = new Map(merged.map(c => [c.id, c]))
    expect(byId.get('builtin:volume')!.visible).toBe(true)
    expect(byId.get('builtin:high')!.visible).toBe(false)
  })
})

// 自选加入信息列 (加入日期 / 加入后涨跌幅): 数据来自 /enriched 的读时计算字段,
// 同样遵循「新增列默认隐藏」契约。列 key 必须与后端返回的字段名逐字一致。
// 不变量: 每个内置列的 source.key 必须同时满足三件事, 否则该列在列表里
// 静默失效 (渲染成 "—"、无法排序、自定义面板里不出现):
//   ① 出现在 COLUMN_GROUPS 的某个分组里 (否则面板里找不到)
//   ② 渲染层能识别 (primitives 有该 case)
//   ③ 排序层能取值 (stock-table.getSortValue 有该 case)
// 曾出过的 bug: builtin:momentum_60d 的 key 写成 'momentum', 三条全不满足,
// 列默认可见却永远显示 "—", 长期无人发现。
describe('watchlist columns: 列 key 不变量 (防止静默失效列)', () => {
  const groupKeys = new Set(COLUMN_GROUPS.flatMap(g => g.keys))
  // 渲染/排序由业务上下文单独处理的 key
  const CONTEXT_KEYS = new Set(['symbol', 'strategies', 'score', 'signals', 'candle', 'intraday', 'action'])

  it('每个内置列的 key 都归属于某个分组', () => {
    for (const col of BUILTIN_COLUMNS) {
      if (col.source.type !== 'builtin') continue
      const key = col.source.key
      if (CONTEXT_KEYS.has(key)) continue
      expect(groupKeys.has(key), `列 ${col.id} 的 key "${key}" 不在任何 COLUMN_GROUPS 中 — 自定义面板里将无法找到该列`).toBe(true)
    }
  })

  it('每个内置列的 key 都能取到排序值 (getSortValue 有对应 case)', () => {
    const r = {
      symbol: '600000.SH', rt_price: 10, close: 10, rt_pct: 0.01, change_pct: 0.01,
      change_amount: 0.1, amplitude: 0.02, open: 10, high: 10.5, low: 9.5, prev_close: 9.9,
      limit_up_price: 11, limit_down_price: 9, volume: 1e6, turnover_rate: 1.5, amount: 1e8,
      float_shares: 1e8, vol_ratio_5d: 1.1, annual_vol_20d: 0.3, ma5: 10, ma10: 10, ma20: 10, ma60: 10,
      high_60d: 12, low_60d: 8, rsi_6: 50, rsi_14: 50, rsi_24: 50, macd_dif: 0, macd_dea: 0, macd_hist: 0,
      kdj_k: 50, kdj_d: 50, kdj_j: 50, boll_upper: 11, boll_lower: 9, atr_14: 0.3,
      vol_ma5: 1e6, vol_ma10: 1e6, momentum_5d: 0.01, momentum_10d: 0.01, momentum_20d: 0.01,
      momentum_30d: 0.01, momentum_60d: 0.01, deviate_3d: 0, deviate_10d: 0, deviate_30d: 0,
      consecutive_limit_ups: 0, consecutive_limit_downs: 0, added_at: '2026-10-01T00:00:00Z',
      pct_since_added: 0.02, score: 1, eps: 1, bps: 1, roe: 0.1, pe_ttm: 10, pb: 1,
      gross_margin: 0.3, net_margin: 0.2, revenue_yoy: 0.1, net_income_yoy: 0.1, debt_ratio: 0.4,
    }
    for (const col of BUILTIN_COLUMNS) {
      if (col.source.type !== 'builtin') continue
      if (CONTEXT_KEYS.has(col.source.key)) continue
      expect(
        getSortValue(r, col),
        `列 ${col.id} 的 key "${col.source.key}" 取不到排序值 (getSortValue 缺 case) — 该列无法排序`,
      ).not.toBeNull()
    }
  })
})

describe('watchlist columns: 自选加入信息列', () => {
  const ADDED_KEYS = ['added_at', 'pct_since_added']

  it('两列已注册且默认隐藏, 并归入「自选」分组', () => {
    for (const key of ADDED_KEYS) {
      const col = BUILTIN_COLUMNS.find(c => c.source.type === 'builtin' && c.source.key === key)
      expect(col, `内置列 ${key} 应存在`).toBeTruthy()
      expect(col!.visible).toBe(false)
    }
    // 必须进 COLUMN_GROUPS, 否则列存在于表格配置但在自定义列面板里不可见
    const group = COLUMN_GROUPS.find(g => g.id === 'added')
    expect(group, '应存在「自选」列分组').toBeTruthy()
    for (const key of ADDED_KEYS) {
      expect(group!.keys).toContain(key)
    }
  })

  it('老用户配置 (不含新列 id) 合并后自动补齐两列且默认隐藏', () => {
    const saved = BUILTIN_COLUMNS
      .filter(c => ['builtin:symbol', 'builtin:price', 'builtin:pct'].includes(c.id))
      .map(c => ({ ...c }))

    const merged = mergeColumns(saved, BUILTIN_COLUMNS)
    const byId = new Map(merged.map(c => [c.id, c]))

    for (const key of ADDED_KEYS) {
      const col = byId.get(`builtin:${key}`)
      expect(col, `合并后应包含新列 builtin:${key}`).toBeTruthy()
      expect(col!.visible).toBe(false)
    }
    // 用户已有列的显隐与顺序不受影响, 新列追加在末尾
    expect(byId.get('builtin:price')!.visible).toBe(true)
    expect(merged[0].id).toBe('builtin:symbol')
    expect(merged.map(c => c.id).slice(-2)).toEqual(['builtin:added_at', 'builtin:pct_since_added'])
  })
})
