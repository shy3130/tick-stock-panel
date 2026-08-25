import { type BacktestRun } from './api.ts'
import { buildRunReportHtml, escapeHtml } from './backtestReport.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message)
  }
}

function countMatches(html: string, pattern: RegExp): number {
  const found = html.match(pattern)
  return found === null ? 0 : found.length
}

/** 每条 <polyline> 的采样点数（用于验证 ≤240 截断） */
function polylinePointCounts(html: string): number[] {
  const counts: number[] = []
  const re = /<polyline[^>]*points="([^"]*)"/g
  let match = re.exec(html)
  while (match !== null) {
    counts.push(match[1].trim().split(/\s+/).filter(part => part !== '').length)
    match = re.exec(html)
  }
  return counts
}

function isoDates(count: number): string[] {
  const start = Date.UTC(2026, 0, 1)
  return Array.from(
    { length: count },
    (_, index) => new Date(start + index * 86400000).toISOString().slice(0, 10)
  )
}

function makeStrategyRun(): BacktestRun {
  const dates = isoDates(30)
  return {
    schema_version: 1,
    run_id: 'run-strategy-001',
    kind: 'strategy',
    created_at: '2026-08-01T10:00:00Z',
    status: 'completed',
    subject: { id: 'st-1', name: '均线突破', hash: 'abc123' },
    config: { start: '2026-01-01', end: '2026-06-30', fees_pct: 0.0002 },
    data_snapshot: {
      data_start: '2024-01-01',
      data_cutoff: '2026-07-01',
      adjustment_mode: 'qfq',
      snapshot_hash: 'deadbeef',
    },
    benchmark: { symbol: 'sh000300', name: '沪深300' },
    cost_model: { fees_pct: 0.0002 },
    metric_context: { version: '1', return_frequency: 'daily', periods_per_year: 244, std_ddof: 1 },
    random_seed: 42,
    engine_version: 'engine-2.1',
    stats: {
      total_return: 0.2531,
      sharpe: 1.62,
      max_drawdown: -0.0812,
      n_trades: 2,
      bad_marker: Number.NaN,
      worse_marker: Number.POSITIVE_INFINITY,
    },
    equity_curve: dates.map((date, index) => ({ date, value: 1 + index * 0.01 })),
    drawdown_curve: dates.map((date, index) => ({ date, value: -0.01 * ((index % 5) + 1) })),
    benchmark_curve: dates.map((date, index) => ({
      date,
      close: 4000 + index * 5,
      name: '沪深300',
      symbol: 'sh000300',
    })),
    trades: [
      {
        symbol: '600519',
        name: '贵州茅台',
        entry_date: dates[1],
        exit_date: dates[10],
        entry_price: 1500,
        exit_price: 1650,
        pnl_pct: 0.1,
        duration: 9,
        exit_reason: 'target',
      },
      {
        symbol: '000001',
        name: '平安银行',
        entry_date: dates[5],
        exit_date: dates[8],
        entry_price: 10,
        exit_price: 9.5,
        pnl_pct: -0.05,
        duration: 3,
        exit_reason: 'stop',
      },
    ],
    per_symbol_stats: [],
    factor_result: null,
    warnings: ['样本外区间较短'],
    favorite: false,
    label: '主线实验',
    source_run_id: null,
  }
}

function testEscapeHtmlBasics() {
  assert(escapeHtml('<script>alert("x")</script>') === '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;', 'tag escape')
  assert(escapeHtml("a'b&c") === 'a&#39;b&amp;c', 'quote and ampersand escape')
  assert(escapeHtml(null) === '', 'null to empty')
  assert(escapeHtml(undefined) === '', 'undefined to empty')
  assert(escapeHtml(Number.NaN) === '', 'NaN to empty')
  assert(escapeHtml(Number.POSITIVE_INFINITY) === '', 'Infinity to empty')
  assert(escapeHtml(42.5) === '42.5', 'finite number kept')
  assert(escapeHtml('') === '', 'empty string stays empty')
}

function testXssEscapedEverywhere() {
  const run = makeStrategyRun()
  run.label = '<script>alert(1)</script>'
  run.subject.name = '<img src=x onerror=alert(2)>'
  run.status = '"><svg onload=alert(3)>'
  run.warnings = ['warn & <b>bold</b>', '</textarea><script>w</script>']
  run.config = { hostile: '" onmouseover="alert(4)' }
  run.benchmark = { symbol: null, name: "'\"><script>alert(5)</script>" }
  run.trades = [
    {
      symbol: '<b>600519</b>',
      name: '<i>贵州茅台</i>',
      entry_date: '2026-01-02',
      exit_date: '2026-01-12',
      entry_price: 1500,
      exit_price: 1650,
      pnl_pct: 0.1,
      duration: 9,
      exit_reason: '"><script>alert(6)</script>',
    },
  ]
  run.data_snapshot = { snapshot_hash: '&"><script>alert(7)</script>' }
  const html = buildRunReportHtml(run)
  assert(!/<script/i.test(html), 'no raw <script> tag may survive')
  assert(!/<(script|img|iframe)\b/i.test(html), 'no raw script/img/iframe tag may survive')
  assert(!/<svg(?!\s+viewBox)/.test(html), 'any raw svg tag must be our chart root only')
  assert(html.includes('&lt;script&gt;'), 'script tag escaped')
  assert(html.includes('&lt;img src=x onerror=alert(2)&gt;'), 'img payload escaped')
  assert(html.includes('&lt;svg onload=alert(3)&gt;'), 'status payload escaped')
  assert(html.includes('&quot; onmouseover=&quot;'), 'config payload quotes escaped so it cannot break out of text')
  assert(html.includes('&quot;'), 'double quotes escaped')
  assert(html.includes('&#39;'), 'single quotes escaped')
  assert(html.includes('&amp;'), 'ampersand escaped')
  assert(html.includes('&lt;b&gt;600519&lt;/b&gt;'), 'trade symbol escaped')
}

function testStrategyReportSections() {
  const html = buildRunReportHtml(makeStrategyRun())
  assert(html.startsWith('<!DOCTYPE html>'), 'doctype present')
  assert(html.includes('<meta charset="utf-8">'), 'charset meta present')
  assert(!/<script/i.test(html), 'report must be script-free')
  assert(html.includes('回测报告 · 主线实验'), 'title includes label')
  for (const id of ['metadata', 'config', 'snapshot', 'metrics', 'warnings', 'curves', 'detail']) {
    assert(html.includes(`id="${id}"`), `section ${id} present`)
  }
  assert(countMatches(html, /<svg/g) === 2, 'equity+benchmark chart and drawdown chart')
  assert(html.includes('策略净值'), 'equity legend present')
  assert(html.includes('基准 · 沪深300'), 'benchmark legend present')
  assert(html.includes('回测报告') && html.includes('离线回测报告'), 'footer present')
  assert(html.includes('25.31%'), 'total_return formatted as pct')
  assert(html.includes('1.6200'), 'sharpe formatted as decimal')
  assert(html.includes('-8.12%'), 'max_drawdown formatted as pct')
  assert(countMatches(html, /<tr class="trade">/g) === 2, 'both trade rows rendered')
  assert(html.includes('平安银行'), 'trade name rendered')
  assert(html.includes('10.00%') && html.includes('-5.00%'), 'trade pnl formatted')
  assert(!html.includes('NaN') && !html.includes('Infinity'), 'non-finite stats filtered')
  assert(html.includes('样本外区间较短'), 'warning rendered')
  assert(html.includes('沪深300'), 'snapshot/config section has content')
}

function makeCandidateRun(): BacktestRun {
  const run = makeStrategyRun()
  run.run_id = 'run-candidate-001'
  run.label = '候选全量执行'
  run.stats = {
    full_kind: 'candidate_execution',
    mode: 'full',
    total_return: 0.1842,
    max_drawdown: -0.062,
    win_rate: 0.55,
    n_trades: 2,
    n_candidates: 8,
  }
  // 即使带了基准曲线，候选报告也不得绘制/标注基准
  return run
}

function testCandidateExecutionReport() {
  const html = buildRunReportHtml(makeCandidateRun())
  assert(html.includes('样本曲线累计'), 'candidate total_return label')
  assert(html.includes('样本曲线回撤'), 'candidate max_drawdown label')
  assert(html.includes('18.42%'), 'candidate total_return value still formatted')
  assert(html.includes('-6.20%'), 'candidate max_drawdown value still formatted')
  assert(html.includes('候选样本收益曲线'), 'candidate curve title')
  assert(html.includes('按退出事件日等权复利，非可交易账户净值。'), 'candidate curve semantics note')
  assert(html.includes('候选样本收益'), 'candidate series legend')
  assert(!html.includes('净值与基准曲线'), 'must not label as equity+benchmark chart')
  assert(!html.includes('策略净值'), 'must not use strategy equity label')
  assert(!html.includes('background:#f59e0b'), 'must not draw a benchmark series')
  // 正常策略标签不得出现在候选指标表（总收益/最大回撤）
  assert(!html.includes('>总收益</'), 'default total_return label absent')
  assert(!html.includes('>最大回撤</'), 'default max_drawdown label absent')
}

function testStrategyReportNotCandidate() {
  // 回归：正常策略报告仍使用账户净值语义，不受候选文案污染
  const html = buildRunReportHtml(makeStrategyRun())
  assert(html.includes('净值与基准曲线'), 'strategy equity+benchmark title kept')
  assert(html.includes('策略净值'), 'strategy equity legend kept')
  assert(html.includes('基准 · 沪深300'), 'benchmark legend kept')
  assert(html.includes('总收益'), 'default total_return label kept')
  assert(html.includes('最大回撤'), 'default max_drawdown label kept')
  assert(!html.includes('样本曲线累计'), 'candidate total_return label absent')
  assert(!html.includes('样本曲线回撤'), 'candidate max_drawdown label absent')
  assert(!html.includes('候选样本收益曲线'), 'candidate curve title absent')
}


function makeFactorRun(): BacktestRun {
  const dates = isoDates(20)
  return {
    schema_version: 1,
    run_id: 'run-factor-001',
    kind: 'factor',
    created_at: '2026-08-02T08:00:00Z',
    status: 'completed',
    subject: { id: 'momentum_20d', name: 'momentum_20d', hash: 'ff00' },
    config: { n_groups: 3, rebalance: 'daily' },
    data_snapshot: { data_start: '2025-01-01', data_cutoff: '2026-07-01', adjustment_mode: 'qfq', snapshot_hash: 'cafe' },
    benchmark: null,
    cost_model: {},
    metric_context: {},
    random_seed: null,
    engine_version: 'engine-2.1',
    stats: {},
    equity_curve: [],
    drawdown_curve: [],
    benchmark_curve: [],
    trades: [],
    per_symbol_stats: [],
    factor_result: {
      ic_mean: 0.043,
      ic_std: 0.02,
      ir: 2.1,
      ic_win_rate: 0.61,
      group_stats: [
        { group: 1, label: '<x>G1', total_return: 0.1, annual_return: null, max_drawdown: -0.05, sharpe: 0.9, win_rate: 0.55, avg_turnover: 0.12, total_turnover: 1.2, total_cost: 0.01 },
        { group: 2, label: 'G2', total_return: -0.02, annual_return: -0.08, max_drawdown: -0.12, sharpe: -0.3, win_rate: 0.44, avg_turnover: 0.12, total_turnover: 1.2, total_cost: 0.01 },
        { group: 3, label: 'G3', total_return: 0.05, annual_return: 0.2, max_drawdown: -0.07, sharpe: 0.5, win_rate: 0.51, avg_turnover: 0.12, total_turnover: 1.2, total_cost: 0.01 },
      ],
      group_nav: [],
      long_short_stats: { total_return: 0.32, sharpe: 1.1 },
      long_short_nav: dates.map((date, index) => ({ date, value: 1 + index * 0.005 })),
      n_symbols: 120,
      n_dates: 20,
    },
    warnings: [],
    favorite: false,
    label: '',
    source_run_id: null,
  }
}

function testFactorReport() {
  const html = buildRunReportHtml(makeFactorRun())
  assert(html.includes('多空净值'), 'long-short legend present')
  assert(html.includes('多空组合'), 'long-short stats table present')
  assert(html.includes('32.00%'), 'long-short total_return as pct')
  assert(html.includes('IC 均值') && html.includes('0.0430'), 'ic_mean merged from factor_result')
  assert(html.includes('2.1000'), 'ir merged from factor_result')
  assert(html.includes('分组统计'), 'group stats section title')
  assert(html.includes('<th>标签</th>'), 'group table label column')
  assert(countMatches(html, /<tr class="group">/g) === 3, 'three group rows')
  assert(countMatches(html, /<tr class="trade">/g) === 0, 'no trade rows for factor run')
  assert(html.includes('&lt;x&gt;G1'), 'hostile group label escaped')
  assert(countMatches(html, /<svg/g) === 1, 'single long-short chart')
}

function testLegacyEquityKeys() {
  const dates = isoDates(15)
  const run = makeStrategyRun()
  run.equity_curve = dates.map((date, index) => ({ date, equity: 2 + index * 0.02, value: undefined }))
  run.benchmark_curve = dates.map((date, index) => ({ date, close: 3000 + index * 3, name: index === 0 ? 'HS300' : undefined }))
  run.benchmark = null
  run.metric_context = {}
  run.random_seed = null
  run.engine_version = ''
  const html = buildRunReportHtml(run)
  const counts = polylinePointCounts(html)
  assert(counts.length >= 2, 'legacy equity and benchmark curves both rendered')
  assert(html.includes('基准 · HS300'), 'benchmark display falls back to curve row name')
  assert(html.includes('策略净值'), 'equity series labeled')
}

function testCurveDownsampleTruncation() {
  const dates = isoDates(500)
  const run = makeStrategyRun()
  run.equity_curve = dates.map((date, index) => ({ date, value: 1 + index * 0.001 }))
  run.drawdown_curve = dates.map((date, index) => ({ date, value: -0.001 * (index % 30) }))
  run.benchmark_curve = dates.map((date, index) => ({ date, value: 1 + index * 0.0005 }))
  const counts = polylinePointCounts(buildRunReportHtml(run))
  assert(counts.length === 3, 'three series rendered')
  for (const count of counts) {
    assert(count >= 2 && count <= 240, `polyline sampled to <=240 points, got ${count}`)
  }
}

function testTradesTruncation() {
  const dates = isoDates(40)
  const run = makeStrategyRun()
  run.trades = Array.from({ length: 250 }, (_, index) => ({
    symbol: `60${String(1000 + index).slice(1)}`,
    name: `股票${index}`,
    entry_date: dates[index % dates.length],
    exit_date: dates[(index + 5) % dates.length],
    entry_price: 10,
    exit_price: 11,
    pnl_pct: 0.1,
    duration: 5,
    exit_reason: 'target',
  }))
  const html = buildRunReportHtml(run)
  assert(countMatches(html, /<tr class="trade">/g) === 200, 'only first 200 trades rendered')
  assert(html.includes('共 250 笔交易，仅展示前 200 笔。'), 'truncation note rendered')
}

function testInvalidCurvesDegrade() {
  const run = makeStrategyRun()
  run.equity_curve = [
    { date: '2026-01-01', value: Number.NaN },
    { date: '2026-01-02', value: Number.POSITIVE_INFINITY },
    { date: '', value: 1 },
    { date: '2026-01-04', value: null },
    { date: '2026-01-05', value: 'not-a-number' },
    { date: '2026-01-06', value: 1.5 },
  ] as unknown as BacktestRun['equity_curve']
  run.drawdown_curve = [{ date: '2026-01-01', value: Number.NaN }]
  run.benchmark_curve = 'not-an-array' as unknown as BacktestRun['benchmark_curve']
  run.trades = []
  const html = buildRunReportHtml(run)
  assert(html.includes('净值曲线数据不足，无法绘图。'), 'invalid equity curve degrades to placeholder')
  assert(countMatches(html, /<svg/g) === 0, 'no chart rendered from invalid curves')
  assert(!html.includes('NaN') && !html.includes('Infinity'), 'non-finite values never leak')
  assert(html.includes('无交易明细。'), 'empty trades degrades to placeholder')
}

function testMissingFieldsPlaceholders() {
  const run = makeStrategyRun()
  run.config = {}
  run.data_snapshot = {}
  run.stats = {}
  run.warnings = []
  run.trades = []
  run.equity_curve = []
  run.drawdown_curve = []
  run.benchmark_curve = []
  run.benchmark = null
  run.label = ''
  const html = buildRunReportHtml(run)
  assert(html.includes('未记录配置信息。'), 'empty config placeholder')
  assert(html.includes('未记录数据快照。'), 'empty snapshot placeholder')
  assert(html.includes('未记录指标。'), 'empty stats placeholder')
  assert(html.includes('无警告。'), 'no warnings placeholder')
  assert(html.includes('净值曲线数据不足，无法绘图。'), 'empty curves placeholder')
  assert(html.includes('无交易明细。'), 'no trades placeholder')
  assert(html.includes('回测报告 · 均线突破'), 'title falls back to subject name')
}

function testInvalidFactorRunDegrades() {
  const run = makeFactorRun()
  run.factor_result = { group_stats: [], long_short_nav: [], long_short_stats: {} }
  const html = buildRunReportHtml(run)
  assert(html.includes('多空净值曲线数据不足，无法绘图。'), 'empty long-short nav placeholder')
  assert(html.includes('无分组统计。'), 'empty group stats placeholder')
  assert(html.includes('未记录指标。'), 'no numeric metrics available')
}

function testTradeAttributionReport() {
  const run = makeStrategyRun()
  run.attribution = {
    status: 'ok',
    scope: '交易窗口、按当前行业分类、相对等权已执行交易样本的 Brinson-Fachler 归因（非官方指数归因）',
    classification_note: '当前分类，不是交易时点分类。',
    input_trades: 3,
    classified_trades: 3,
    capital_coverage: 1,
    warnings: ['行业映射来自当前快照'],
    brinson: {
      status: 'ok',
      normalized: true,
      portfolio_return: 0.04,
      benchmark_return: 0.02,
      excess_return: 0.02,
      allocation: 0.005,
      selection: 0.01,
      interaction: 0.005,
      total_effect: 0.02,
      groups: [{
        group: '<img src=x onerror=alert(1)>',
        portfolio_weight: 0.6,
        benchmark_weight: 0.5,
        portfolio_return: 0.05,
        benchmark_return: 0.02,
        allocation: 0.005,
        selection: 0.01,
        interaction: 0.005,
        total_effect: 0.02,
      }],
    },
    fama_french: {
      status: 'unavailable',
      reason: 'factor_return_series_unavailable',
      detail: '没有冻结且可审计的本地因子收益序列',
      alpha: null,
      betas: {},
      contributions: {},
      r_squared: null,
      residual_volatility: null,
      observations: 0,
    },
  }
  const html = buildRunReportHtml(run)

  assert(html.includes('id=\"attribution\"'), 'attribution section present')
  assert(html.includes('交易窗口行业归因'), 'attribution title rendered')
  assert(html.includes('非官方指数归因'), 'scope rendered')
  assert(html.includes('4.00%') && html.includes('2.00%'), 'attribution metrics formatted')
  assert(html.includes('Fama-French：没有冻结且可审计的本地因子收益序列'), 'explicit unavailable factor note')
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'), 'industry label escaped')
  assert(!/<img\b/i.test(html), 'hostile industry label cannot create markup')
}

const tests = [
  testEscapeHtmlBasics,
  testXssEscapedEverywhere,
  testStrategyReportSections,
  testCandidateExecutionReport,
  testStrategyReportNotCandidate,
  testFactorReport,
  testLegacyEquityKeys,
  testCurveDownsampleTruncation,
  testTradesTruncation,
  testInvalidCurvesDegrade,
  testMissingFieldsPlaceholders,
  testInvalidFactorRunDegrades,
  testTradeAttributionReport,
]


let failed = 0
for (const run of tests) {
  try {
    run()
    console.log(`PASS ${run.name}`)
  } catch (e) {
    failed += 1
    console.error(`FAIL ${run.name}: ${(e as Error).message}`)
  }
}
if (failed > 0) {
  console.error(`${failed}/${tests.length} tests failed`)
  process.exit(1)
}
console.log(`${tests.length}/${tests.length} tests passed`)
