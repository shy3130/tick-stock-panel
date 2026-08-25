// 回测可信度诊断 — 纯逻辑 (无 React/ECharts 依赖), 供结果面板复用与单测覆盖。
//
// 覆盖三块数据变换:
// - 成本敏感性倍数表排序 (倍数升序, 不修改入参);
// - 逐笔 bootstrap 净值带形状校验 (分位数组等长 = n_trades, 最终分位为有限数);
// - 市场状态桶 → 2x2 网格单元的数据变换 (含缺桶兜底与指标 null 透传);
// - 最大参与率百分数输入解析 (空 = 关闭, 0 < x <= 100 → 0-1 小数)。

import type {
  CostSensitivityRow,
  RegimeBucketKey,
  RegimeBucketStats,
  TradeEquityBand,
} from '../../../lib/api'

const finiteNumber = (value: unknown): number | null => {
  if (value == null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** 成本敏感性行按倍数升序排序 (复制排序, 不改入参); 倍数缺失/非有限的行沉底保持稳定顺序 */
export function sortCostRows(rows: CostSensitivityRow[]): CostSensitivityRow[] {
  return [...rows].sort((left, right) => {
    const a = finiteNumber(left?.multiplier)
    const b = finiteNumber(right?.multiplier)
    if (a == null && b == null) return 0
    if (a == null) return 1
    if (b == null) return -1
    return a - b
  })
}

const BAND_PERCENTILE_KEYS = ['p05', 'p25', 'p50', 'p75', 'p95'] as const
type BandPercentileKey = (typeof BAND_PERCENTILE_KEYS)[number]

/**
 * 逐笔 bootstrap 净值带形状校验 — 旧持久化/截断响应不得进入图表:
 * - n_trades/n_boot/seed 为有限数, 且 n_trades > 0;
 * - 五条分位数组均为等长 (== n_trades) 的有限数数组;
 * - final_value 五分位均为有限数。
 * 任一不满足返回 null (fail-closed, 调用方按数据缺失处理)。
 */
export function validateTradeEquityBand(band: unknown): TradeEquityBand | null {
  if (band == null || typeof band !== 'object') return null
  const raw = band as Record<string, unknown>
  const nTrades = finiteNumber(raw.n_trades)
  if (nTrades == null || !Number.isInteger(nTrades) || nTrades <= 0) return null
  if (finiteNumber(raw.n_boot) == null || finiteNumber(raw.seed) == null) return null
  const percentiles = raw.percentiles
  if (percentiles == null || typeof percentiles !== 'object') return null
  const pRaw = percentiles as Record<string, unknown>
  const series: Record<BandPercentileKey, number[]> = {} as Record<BandPercentileKey, number[]>
  for (const key of BAND_PERCENTILE_KEYS) {
    const column = pRaw[key]
    if (!Array.isArray(column) || column.length !== nTrades) return null
    const values: number[] = []
    for (const item of column) {
      const parsed = finiteNumber(item)
      if (parsed == null) return null
      values.push(parsed)
    }
    series[key] = values
  }
  const finalsRaw = raw.final_value_percentiles
  if (finalsRaw == null || typeof finalsRaw !== 'object') return null
  const finals: Record<BandPercentileKey, number> = {} as Record<BandPercentileKey, number>
  for (const key of BAND_PERCENTILE_KEYS) {
    const parsed = finiteNumber((finalsRaw as Record<string, unknown>)[key])
    if (parsed == null) return null
    finals[key] = parsed
  }
  return {
    n_trades: nTrades,
    n_boot: finiteNumber(raw.n_boot) as number,
    seed: finiteNumber(raw.seed) as number,
    percentiles: series,
    final_value_percentiles: finals,
  }
}

/** 市场状态 2x2 网格单元 — 桶键 + 中文标签 + 展示用扁平字段 */
export interface RegimeGridCell {
  key: RegimeBucketKey
  label: string
  trend: 'bull' | 'bear'
  vol: 'turbulent' | 'calm'
  days: number
  daysPct: number | null
  strategyTotalReturn: number | null
  strategyAnnualizedReturn: number | null
  strategySharpe: number | null
  strategyMaxDrawdown: number | null
  benchmarkTotalReturn: number | null
  excessTotalReturn: number | null
}

const REGIME_GRID_ORDER: Array<{ key: RegimeBucketKey; label: string; trend: 'bull' | 'bear'; vol: 'turbulent' | 'calm' }> = [
  { key: 'bull_turbulent', label: '牛市 · 高波动', trend: 'bull', vol: 'turbulent' },
  { key: 'bull_calm', label: '牛市 · 平静', trend: 'bull', vol: 'calm' },
  { key: 'bear_turbulent', label: '熊市 · 高波动', trend: 'bear', vol: 'turbulent' },
  { key: 'bear_calm', label: '熊市 · 平静', trend: 'bear', vol: 'calm' },
]

/**
 * 桶 → 固定顺序 (牛高/牛平/熊高/熊平) 的网格单元; buckets 缺失返回 []。
 * 缺桶按"天数 0 + 指标 null"兜底 (后端四桶恒全量返回, 此处仅防御旧响应)。
 */
export function buildRegimeGrid(buckets: Record<RegimeBucketKey, RegimeBucketStats> | null | undefined): RegimeGridCell[] {
  if (buckets == null || typeof buckets !== 'object') return []
  return REGIME_GRID_ORDER.map(({ key, label, trend, vol }) => {
    const bucket = buckets[key] as RegimeBucketStats | undefined
    return {
      key,
      label,
      trend,
      vol,
      days: finiteNumber(bucket?.days) ?? 0,
      daysPct: finiteNumber(bucket?.days_pct),
      strategyTotalReturn: finiteNumber(bucket?.strategy_total_return),
      strategyAnnualizedReturn: finiteNumber(bucket?.strategy_annualized_return),
      strategySharpe: finiteNumber(bucket?.strategy_sharpe),
      strategyMaxDrawdown: finiteNumber(bucket?.strategy_max_drawdown),
      benchmarkTotalReturn: finiteNumber(bucket?.benchmark_total_return),
      excessTotalReturn: finiteNumber(bucket?.excess_total_return),
    }
  })
}

export interface ParticipationPctParse {
  ok: boolean
  /** 0-1 小数; 空输入时为 null (关闭量能约束) */
  value: number | null
  error?: string
}

/**
 * 最大参与率输入解析: 空/纯空白 → {ok, null} (关闭);
 * 0 < x <= 100 → {ok, x/100}; 其余 (0、>100、非数) → {ok: false, error}。
 */
export function parseParticipationPctInput(raw: string | null | undefined): ParticipationPctParse {
  const text = (raw ?? '').trim()
  if (text === '') return { ok: true, value: null }
  const parsed = Number(text)
  if (!Number.isFinite(parsed) || parsed <= 0 || parsed > 100) {
    return { ok: false, value: null, error: '最大参与率必须为空（关闭）或 (0, 100] 之间的百分数' }
  }
  return { ok: true, value: parsed / 100 }
}
