import type { BacktestRunComparison, BacktestRunSummary } from './api.ts'
import { buildCompareReportFilename } from './backtestReportDownload.ts'
import {
  COMPARE_COLORS,
  buildCompareReportHtml,
  formatDeltaValue,
  formatMetricValue,
} from './compareReport.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`断言失败: ${message}`)
}

function countMatches(html: string, pattern: RegExp): number {
  return (html.match(pattern) ?? []).length
}

function isoDates(count: number): string[] {
  const out: string[] = []
  const base = Date.UTC(2024, 0, 1)
  for (let i = 0; i < count; i += 1) {
    out.push(new Date(base + i * 86400_000).toISOString().slice(0, 10))
  }
  return out
}

function makeSummary(index: number, overrides: Partial<BacktestRunSummary> = {}): BacktestRunSummary {
  return {
    run_id: `runid${String(index).padStart(2, '0')}aa`,
    kind: 'strategy',
    status: 'completed',
    created_at: `2026-08-0${(index % 9) + 1}T10:00:00`,
    subject: { id: `st-${index}`, name: `策略${index}`, hash: `hash${index}` },
    start: '2024-01-01',
    end: '2024-12-31',
    symbols_count: 10,
    favorite: false,
    label: '',
    source_run_id: null,
    stats: {},
    n_trades: 5 + index,
    n_points: 100,
    has_factor_result: false,
    has_csv_export: true,
    warnings_count: 0,
    ...overrides,
  }
}

interface ComparisonOverrides {
  sharpeFor?: (index: number) => number | null
  curveFor?: (index: number) => { date: string; value: number }[]
  configDiff?: BacktestRunComparison['config_diff']
  tradeSummary?: BacktestRunComparison['trade_summary']
  warnings?: string[]
}

function makeComparison(runCount: number, overrides: ComparisonOverrides = {}): BacktestRunComparison {
  const runs = Array.from({ length: runCount }, (_, i) => makeSummary(i))
  const ids = runs.map(run => run.run_id)
  const totalReturn: Record<string, number | null> = {}
  const sharpe: Record<string, number | null> = {}
  const maxDrawdown: Record<string, number | null> = {}
  ids.forEach((id, i) => {
    totalReturn[id] = 0.1 * (i + 1)
    sharpe[id] = overrides.sharpeFor ? overrides.sharpeFor(i) : 1 + i * 0.2
    maxDrawdown[id] = -0.05 * (i + 1)
  })
  const defaultCurve = (index: number) =>
    isoDates(30).map((date, j) => ({ date, value: 100000 * (1 + (index + 1) * 0.001 * j) }))
  return {
    runs,
    metric_matrix: { total_return: totalReturn, sharpe, max_drawdown: maxDrawdown },
    curves: runs.map((run, index) => ({
      run_id: run.run_id,
      kind: run.kind,
      equity_curve: overrides.curveFor ? overrides.curveFor(index) : defaultCurve(index),
      benchmark_curve: [],
    })),
    warnings: overrides.warnings ?? ['compare.different_universes: 标的池不一致，指标仅作参考'],
    ...(overrides.configDiff !== undefined ? { config_diff: overrides.configDiff } : {}),
    ...(overrides.tradeSummary !== undefined ? { trade_summary: overrides.tradeSummary } : {}),
  }
}

function testSharedFormatters() {
  assert(formatMetricValue('total_return', 0.12) === '+12.00%', '百分比指标应输出 +12.00%')
  assert(formatMetricValue('total_return', null) === '—', 'null 指标应显示 —')
  assert(formatMetricValue('total_return', Number.NaN) === '—', 'NaN 指标应显示 —')
  assert(formatMetricValue('sharpe', 0.0567) === '0.0567', '小数量级数值指标保留 4 位')
  assert(formatMetricValue('n_trades', 12.6) === '13', 'int 指标取整')
  assert(formatDeltaValue('total_return', 0.03) === '+3.00%', '百分比差值自带正号')
  assert(formatDeltaValue('sharpe', 0.4) === '+0.40', '数值差值手动补正号')
  assert(COMPARE_COLORS.length >= 8, '调色板应覆盖 8 个 run 不重复')
}

function testReportBasics() {
  const html = buildCompareReportHtml(makeComparison(3), new Date(2026, 7, 20, 14, 30))
  assert(html.startsWith('<!DOCTYPE html>'), '报告应以 DOCTYPE 开头')
  assert(html.includes('<html lang="zh-CN">'), '语言应为 zh-CN')
  assert(html.includes('<title>回测对比报告 · 3 个运行</title>'), '标题应包含运行数量')
  assert(html.includes('生成时间 2026-08-20 14:30'), '元信息应包含生成时间')
  for (let i = 0; i < 3; i += 1) {
    assert(html.includes(`策略${i}`), `Run 列表应包含 策略${i}`)
  }
  assert(html.includes('回测区间'), 'Run 列表应包含区间列')
  assert(html.includes('指标矩阵'), '应包含指标矩阵区块')
  assert(html.includes('配置差异'), '应包含配置差异区块')
  assert(html.includes('交易变动摘要'), '应包含交易变动摘要区块')
  assert(html.includes('归一化净值曲线'), '应包含归一化净值曲线区块')
  assert(html.includes('可比性提醒'), '应包含可比性警告区块')
  assert(html.includes('标的池不一致，指标仅作参考'), '警告应去掉 compare. 机器前缀')
  assert(html.includes('<polyline'), '净值曲线应使用内联 SVG polyline')
  assert(!html.includes('echarts'), '自包含报告不得引用 ECharts')
  assert(html.includes('离线对比报告 · 自包含 HTML'), '页脚应含自包含说明')
  assert(html.includes('方法论提醒'), '页脚应含方法论警告')
  assert(html.includes('Δ = 对比值 − 基线值'), '页脚应含 Δ 口径说明')
  assert(html.includes('（基线）'), '首个 run 应标注基线')
  assert(countMatches(html, /class="delta">基线<\/span>/g) === 3, '矩阵每行首列应标基线')
}

function testNonFiniteValuesRenderDash() {
  const html = buildCompareReportHtml(
    makeComparison(2, { sharpeFor: index => (index === 1 ? null : 1.2) }),
    new Date(2026, 7, 20, 9, 0),
  )
  assert(html.includes('Δ —'), '非有限值差值应显示 Δ —')
  // sharpe 列：基线 1.20，对比 — ；行内同时存在数值与占位
  const sharpeRow = html.slice(html.indexOf('夏普比率'), html.indexOf('夏普比率') + 400)
  assert(sharpeRow.includes('—'), '夏普行应包含占位符 —')
  assert(!sharpeRow.includes('NaN'), '不得出现 NaN')
  assert(!html.includes('Infinity'), '不得出现 Infinity')
}

function testEightRunsNoOverflow() {
  const html = buildCompareReportHtml(makeComparison(8), new Date(2026, 7, 20, 23, 5))
  // 8 条有效曲线 → 8 条 polyline，颜色循环不塌缩
  assert(countMatches(html, /<polyline /g) === 8, '8 个 run 应绘制 8 条 polyline')
  for (const color of COMPARE_COLORS.slice(0, 8)) {
    assert(html.includes(`stroke="${color}"`), `曲线应包含颜色 ${color}`)
  }
  // 矩阵表设置足够 min-width，宽表靠横向滚动而非挤压
  assert(
    html.includes('style="min-width:1236px"'),
    '8 列矩阵 min-width 应为 180 + 8*132 = 1236px',
  )
  assert(html.includes('table-wrap'), '矩阵应置于横向滚动容器内')
  assert(html.includes('<title>回测对比报告 · 8 个运行</title>'), '标题应显示 8 个运行')
  // 8 个 run 各占一列：表头 run 圆点应有 8 个
  assert(countMatches(html, /class="run-dot"/g) === 16, '元信息 8 + 矩阵 8 = 16 个颜色圆点')
}

function testFilename() {
  assert(
    buildCompareReportFilename(new Date(2026, 0, 1, 9, 30)) === '回测对比_20260101_0930.html',
    '文件名应为 回测对比_YYYYMMDD_HHmm.html',
  )
  assert(
    buildCompareReportFilename(new Date(2026, 11, 31, 23, 59)) === '回测对比_20261231_2359.html',
    '月/日/时/分应补零',
  )
}

function testXssEscaped() {
  const malicious = makeSummary(0, { label: '<script>alert(1)</script>' })
  const comparison = makeComparison(2)
  comparison.runs[0] = malicious
  const html = buildCompareReportHtml(comparison, new Date(2026, 7, 20, 10, 0))
  assert(!html.includes('<script>alert(1)</script>'), '标签文本必须转义，不得出现原始 script')
  assert(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), '应输出转义后的标签')
}

function testSkippedCurvesNote() {
  const html = buildCompareReportHtml(
    makeComparison(3, {
      curveFor: index => (index === 1 ? [] : isoDates(20).map((date, j) => ({ date, value: 100 + j }))),
    }),
    new Date(2026, 7, 20, 11, 0),
  )
  assert(countMatches(html, /<polyline /g) === 2, '空曲线 run 不参与绘图')
  assert(html.includes('策略1 无账户净值曲线（因子/旧记录），未参与绘图。'), '应说明跳过的 run')
}

function testAllCurvesMissing() {
  const html = buildCompareReportHtml(
    makeComparison(2, { curveFor: () => [] }),
    new Date(2026, 7, 20, 11, 0),
  )
  assert(!html.includes('<polyline'), '无有效曲线时不应输出 polyline')
  assert(html.includes('所选 run 均无账户净值曲线'), '应给出无曲线占位说明')
}

function testMissingOptionalSections() {
  const html = buildCompareReportHtml(makeComparison(2), new Date(2026, 7, 20, 12, 0))
  assert(
    html.includes('后端响应未包含配置差异（旧版本对比接口）。'),
    '缺 config_diff 应有占位说明',
  )
  assert(
    html.includes('后端响应未包含交易变动摘要（旧版本对比接口）。'),
    '缺 trade_summary 应有占位说明',
  )
}

function testConfigDiffAndTradeSummary() {
  const ids = ['runid00aa', 'runid01aa']
  const html = buildCompareReportHtml(
    makeComparison(2, {
      configDiff: {
        baseline_run_id: ids[0],
        candidates: [
          {
            run_id: ids[1],
            total: 2,
            truncated: true,
            entries: [
              { path: 'params.stop_loss', op: 'changed', before: 0.05, after: 0.08 },
              { path: 'symbols', op: 'removed', before: ['000001'], after: null },
            ],
          },
        ],
      },
      tradeSummary: {
        baseline_run_id: ids[0],
        baseline_n_trades: 5,
        candidates: [
          {
            run_id: ids[1],
            n_trades: 6,
            common: 4,
            common_value_diff: 1,
            added: 2,
            removed: 1,
            samples: {
              common: [
                {
                  symbol: '000001',
                  entry_date: '2024-01-05',
                  exit_date: '2024-02-05',
                  value_differs: true,
                  baseline: { shares: 100, entry_value: 1000, exit_value: 1100, pnl_pct: 0.1 },
                  candidate: { shares: 200, entry_value: 2000, exit_value: 2100, pnl_pct: 0.05 },
                },
              ],
              added: [
                {
                  symbol: '600000',
                  entry_date: '2024-01-10',
                  exit_date: null,
                  shares: null,
                  entry_value: null,
                  exit_value: null,
                  pnl_pct: null,
                },
              ],
              removed: [
                {
                  symbol: '300750',
                  entry_date: '2024-01-12',
                  exit_date: '2024-01-20',
                  shares: 50,
                  entry_value: 5000,
                  exit_value: 4800,
                  pnl_pct: -0.04,
                },
              ],
            },
          },
        ],
      },
    }),
    new Date(2026, 7, 20, 13, 0),
  )
  assert(html.includes('共 2 项差异'), '配置差异应显示条数')
  assert(html.includes('差异较多，仅展示前 2 项（共 2 项）。'), '截断提示应渲染')
  assert(html.includes('params.stop_loss'), '配置路径应直出')
  assert(html.includes('row-diff'), '份额/金额不同的共同交易应有底色标记')
  assert(html.includes('新增交易（相对基线）'), '新增交易表应渲染')
  assert(html.includes('消失交易（相对基线）'), '消失交易表应渲染')
  assert(html.includes('共同 4'), '交易变动计数应渲染')
  // null 份额/收益不伪装成 0
  const addedRow = html.slice(html.indexOf('600000'), html.indexOf('600000') + 300)
  assert(addedRow.includes('—'), 'null 份额/收益应显示 —')
  assert(!addedRow.includes('>0<') && !addedRow.includes('+0.00%'), 'null 不得伪装为 0')
}

const tests = [
  testSharedFormatters,
  testReportBasics,
  testNonFiniteValuesRenderDash,
  testEightRunsNoOverflow,
  testFilename,
  testXssEscaped,
  testSkippedCurvesNote,
  testAllCurvesMissing,
  testMissingOptionalSections,
  testConfigDiffAndTradeSummary,
]

let failed = 0
for (const run of tests) {
  try {
    run()
    console.log(`ok - ${run.name}`)
  } catch (error) {
    failed += 1
    console.error(`FAIL - ${run.name}: ${error instanceof Error ? error.message : String(error)}`)
  }
}

if (failed > 0) {
  console.error(`${failed}/${tests.length} tests failed`)
  process.exit(1)
}
console.log(`${tests.length}/${tests.length} tests passed`)
