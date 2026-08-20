// trustDiagnosticsCore 纯逻辑单测 — bun 直跑: bun src/pages/backtest/components/trustDiagnosticsCore.test.ts
// 参考 runStatus.test.ts / navPresentation.test.ts 的自执行断言风格。

import {
  buildRegimeGrid,
  parseParticipationPctInput,
  sortCostRows,
  validateTradeEquityBand,
} from './trustDiagnosticsCore.ts'
import type { CostSensitivityRow, RegimeBucketStats } from '../../../lib/api'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function assertClose(actual: number | null, expected: number, message: string, eps = 1e-9): void {
  assert(actual != null && Math.abs(actual - expected) < eps, `${message} (actual=${actual})`)
}

const bucket = (days: number, daysPct: number, ret: number | null): RegimeBucketStats => ({
  days,
  days_pct: daysPct,
  strategy_total_return: ret,
  strategy_annualized_return: null,
  strategy_sharpe: null,
  strategy_max_drawdown: null,
  benchmark_total_return: null,
  excess_total_return: null,
})

function testSortCostRowsAscending(): void {
  const rows: CostSensitivityRow[] = [
    { multiplier: 3, fees_pct: 0.0006, slippage_bps: 15, is_baseline: false, total_return: 0.1, annualized_return: null, sharpe: 1, max_drawdown: null, final_equity: null, total_cost: null, n_trades: null },
    { multiplier: 0.5, fees_pct: 0.0001, slippage_bps: 2.5, is_baseline: false, total_return: 0.3, annualized_return: null, sharpe: 2, max_drawdown: null, final_equity: null, total_cost: null, n_trades: null },
    { multiplier: 1, fees_pct: 0.0002, slippage_bps: 5, is_baseline: true, total_return: 0.2, annualized_return: null, sharpe: 1.5, max_drawdown: null, final_equity: null, total_cost: null, n_trades: null },
  ]
  const sorted = sortCostRows(rows)
  assert(sorted.map(row => row.multiplier).join(',') === '0.5,1,3', '倍数必须升序')
  assert(sorted[1].is_baseline, '基线行标记需保留')
  assert(rows[0].multiplier === 3, '入参数组不得被修改')
}

function testSortCostRowsInvalidMultiplierSinks(): void {
  const rows = [
    { multiplier: Number.NaN, fees_pct: 0, slippage_bps: 0, is_baseline: false },
    { multiplier: 2, fees_pct: 0, slippage_bps: 0, is_baseline: false },
  ] as unknown as CostSensitivityRow[]
  const sorted = sortCostRows(rows)
  assert(sorted.length === 2 && sorted[0].multiplier === 2, '非有限倍数沉底且不丢行')
  assert(sortCostRows([]).length === 0, '空表返回空')
}

function testValidateTradeEquityBandAcceptsValid(): void {
  const band = {
    n_trades: 3,
    n_boot: 200,
    seed: 7,
    percentiles: {
      p05: [0.9, 0.85, 0.8],
      p25: [1.0, 0.95, 0.9],
      p50: [1.1, 1.05, 1.0],
      p75: [1.2, 1.15, 1.1],
      p95: [1.3, 1.25, 1.2],
    },
    final_value_percentiles: { p05: 0.8, p25: 0.9, p50: 1.0, p75: 1.1, p95: 1.2 },
  }
  const validated = validateTradeEquityBand(band)
  assert(validated != null, '合法 band 必须通过校验')
  assert(validated.percentiles.p50.length === 3, '分位数组长度保留')
  assertClose(validated.final_value_percentiles.p95, 1.2, '最终分位保留')
}

function testValidateTradeEquityBandRejectsMismatchedLength(): void {
  const band = {
    n_trades: 3,
    n_boot: 200,
    seed: 7,
    percentiles: {
      p05: [0.9, 0.85],
      p25: [1.0, 0.95, 0.9],
      p50: [1.1, 1.05, 1.0],
      p75: [1.2, 1.15, 1.1],
      p95: [1.3, 1.25, 1.2],
    },
    final_value_percentiles: { p05: 0.8, p25: 0.9, p50: 1.0, p75: 1.1, p95: 1.2 },
  }
  assert(validateTradeEquityBand(band) == null, '分位数组长度 ≠ n_trades 必须拒绝')
}

function testValidateTradeEquityBandRejectsBadShapes(): void {
  assert(validateTradeEquityBand(null) == null, 'null 拒绝')
  assert(validateTradeEquityBand({ n_trades: 0 }) == null, 'n_trades=0 拒绝')
  assert(validateTradeEquityBand({ n_trades: 2, percentiles: { p05: 'x' } }) == null, '非数组分位拒绝')
  assert(
    validateTradeEquityBand({
      n_trades: 2,
      n_boot: 100,
      seed: 0,
      percentiles: {
        p05: [1, 1],
        p25: [1, 1],
        p50: [1, 1],
        p75: [1, 1],
        p95: [1, 1],
      },
      final_value_percentiles: { p05: 1, p25: 1, p50: 1, p75: 1, p95: Number.NaN },
    }) == null,
    '最终分位含 NaN 拒绝',
  )
}

function testBuildRegimeGridOrderAndFallback(): void {
  const cells = buildRegimeGrid({
    bull_turbulent: bucket(40, 0.4, 0.15),
    bull_calm: bucket(30, 0.3, 0.1),
    bear_turbulent: bucket(20, 0.2, -0.05),
    bear_calm: bucket(10, 0.1, null),
  })
  assert(cells.length === 4, '恒输出 2x2 四格')
  assert(
    cells.map(cell => cell.key).join(',') === 'bull_turbulent,bull_calm,bear_turbulent,bear_calm',
    '网格顺序固定为 牛高/牛平/熊高/熊平',
  )
  assert(cells[0].label === '牛市 · 高波动', '中文标签映射')
  assertClose(cells[0].daysPct as number, 0.4, 'days_pct 透传')
  assertClose(cells[0].strategyTotalReturn as number, 0.15, '策略收益透传')
  assert(cells[3].strategyTotalReturn == null, '指标 null 原样透传不伪造')
  // 缺桶兜底
  const partial = buildRegimeGrid({ bull_calm: bucket(10, 1, 0) } as Record<string, RegimeBucketStats>)
  assert(partial.length === 4 && partial[0].days === 0, '缺桶按天数 0 兜底')
  assert(buildRegimeGrid(null).length === 0, 'buckets 为 null 返回空数组')
}

function testParseParticipationPctInput(): void {
  assert(parseParticipationPctInput('').value == null && parseParticipationPctInput('').ok, '空输入 = 关闭')
  assert(parseParticipationPctInput('   ').value == null, '纯空白 = 关闭')
  assertClose(parseParticipationPctInput('10').value as number, 0.1, '10% → 0.10')
  assertClose(parseParticipationPctInput('12.5').value as number, 0.125, '12.5% → 0.125')
  assertClose(parseParticipationPctInput('100').value as number, 1, '100% → 1.0 (边界含)')
  assert(!parseParticipationPctInput('0').ok, '0 拒绝')
  assert(!parseParticipationPctInput('100.1').ok, '>100 拒绝')
  assert(!parseParticipationPctInput('abc').ok, '非数拒绝')
}

const tests: Array<() => void> = [
  testSortCostRowsAscending,
  testSortCostRowsInvalidMultiplierSinks,
  testValidateTradeEquityBandAcceptsValid,
  testValidateTradeEquityBandRejectsMismatchedLength,
  testValidateTradeEquityBandRejectsBadShapes,
  testBuildRegimeGridOrderAndFallback,
  testParseParticipationPctInput,
]

let failed = 0
for (const test of tests) {
  try {
    test()
    console.log(`PASS ${test.name}`)
  } catch (error) {
    failed += 1
    console.error(`FAIL ${test.name}: ${error instanceof Error ? error.message : String(error)}`)
  }
}

if (failed > 0) process.exit(1)
console.log(`${tests.length}/${tests.length} tests passed`)
