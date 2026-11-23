// 无本地数据判定 — 预热期不得误判"无数据" (v0.3.20 回归)
//
// 回归复现: 启动时 enriched 指标缓存异步预热 (低配机 50s+), 期间后端
// /api/data/status 走 busy 降级 — stale 缓存为空时 daily/enriched 返回 null。
// 旧判定把 null 一律当 trading_days=0, 导致升级后有数据的老用户打开客户端
// 即被误弹"当前暂无数据 / 立即获取数据"引导卡与欢迎弹窗。
import { describe, expect, it } from 'vitest'
import { resolveHasNoData } from './Dashboard'
import type { DataStatus } from '@/lib/api'

function ds(partial: Partial<DataStatus>): DataStatus {
  return partial as DataStatus
}

describe('resolveHasNoData', () => {
  it('status 未返回 → 不判无数据', () => {
    expect(resolveHasNoData(undefined)).toBe(false)
  })

  it('预热中 (indicators_ready=false) 两表为 null → 不判无数据 (回归复现)', () => {
    const s = ds({ indicators_ready: false, daily: undefined, enriched: undefined })
    expect(resolveHasNoData(s)).toBe(false)
  })

  it('预热中即使 stale 缓存有旧值也保持未知态 → 不判无数据', () => {
    const s = ds({
      indicators_ready: false,
      daily: { trading_days: 0 } as DataStatus['daily'],
      enriched: { trading_days: 0 } as DataStatus['enriched'],
    })
    expect(resolveHasNoData(s)).toBe(false)
  })

  it('预热完成但同步/自动修复运行中, 空统计仍为未知态', () => {
    expect(resolveHasNoData(ds({
      indicators_ready: true, stats_pending: true, daily: null, enriched: null,
    }))).toBe(false)
  })

  it('同步运行中即使 stale 统计为 0 也不弹首用引导', () => {
    expect(resolveHasNoData(ds({
      indicators_ready: true, stats_pending: true,
      daily: { trading_days: 0 } as DataStatus['daily'], enriched: null,
    }))).toBe(false)
  })

  it('同步结束且两表均为空时保留新用户引导', () => {
    expect(resolveHasNoData(ds({
      indicators_ready: true, stats_pending: false, daily: null, enriched: null,
    }))).toBe(true)
  })

  it('预热完成且两表交易日为 0 → 判无数据 (新用户正常引导)', () => {
    const s = ds({
      indicators_ready: true,
      daily: { trading_days: 0 } as DataStatus['daily'],
      enriched: { trading_days: 0 } as DataStatus['enriched'],
    })
    expect(resolveHasNoData(s)).toBe(true)
  })

  it('老版本后端无 indicators_ready 字段: 两表 0 仍判无数据 (兼容)', () => {
    const s = ds({
      daily: { trading_days: 0 } as DataStatus['daily'],
      enriched: { trading_days: 0 } as DataStatus['enriched'],
    })
    expect(resolveHasNoData(s)).toBe(true)
  })

  it('enriched 有交易日 → 有数据', () => {
    const s = ds({
      indicators_ready: true,
      enriched: { trading_days: 243 } as DataStatus['enriched'],
      daily: undefined,
    })
    expect(resolveHasNoData(s)).toBe(false)
  })

  it('daily 有交易日 (enriched 未出) → 有数据', () => {
    const s = ds({
      indicators_ready: true,
      daily: { trading_days: 240 } as DataStatus['daily'],
      enriched: undefined,
    })
    expect(resolveHasNoData(s)).toBe(false)
  })
})
