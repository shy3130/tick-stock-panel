import type { BacktestRun } from './api.ts'

/**
 * 回测 Run 独立 HTML 报告生成器（纯函数、零运行时依赖）。
 *
 * 输出为自包含单文件 HTML：内联 CSS、内联 SVG 曲线，无 <script>、无外链资源，
 * 可直接离线打开 / 打印 / 存档。所有动态字符串经 escapeHtml 转义，
 * 非有限数值（NaN / ±Infinity）一律过滤或降级为占位符。
 */

const MAX_CURVE_POINTS = 240
const MAX_TRADE_ROWS = 200

const CHART_WIDTH = 720
const EQUITY_CHART_HEIGHT = 240
const DRAWDOWN_CHART_HEIGHT = 140

const COLOR_EQUITY = '#2563eb'
const COLOR_BENCHMARK = '#f59e0b'
const COLOR_DRAWDOWN = '#dc2626'
const COLOR_LONG_SHORT = '#7c3aed'

const KIND_LABELS: Record<string, string> = {
  strategy: '策略回测',
  factor: '因子分层',
  composite: '组合回测',
}

const METRIC_LABELS: Record<string, string> = {
  total_return: '总收益',
  annual_return: '年化收益',
  benchmark_return: '基准收益',
  excess: '超额收益',
  alpha: 'Alpha',
  sharpe: '夏普比率',
  sortino: 'Sortino',
  calmar: 'Calmar',
  information_ratio: '信息比率',
  omega: 'Omega',
  profit_factor: '利润因子',
  payoff_ratio: '盈亏比',
  tail_ratio: '尾部比率',
  max_drawdown: '最大回撤',
  annual_volatility: '年化波动',
  downside_deviation: '下行波动',
  tracking_error: '跟踪误差',
  ulcer_index: 'Ulcer Index',
  recovery_factor: '恢复因子',
  win_rate: '胜率',
  avg_turnover: '平均换手',
  total_turnover: '累计换手',
  max_exposure: '最大敞口',
  avg_duration: '平均持仓天数',
  n_trades: '交易数',
  pending_exit_positions: '未完成退出',
  ic_mean: 'IC 均值',
  ic_std: 'IC 标准差',
  ir: 'IR',
  ic_win_rate: 'IC 胜率',
  n_symbols: '标的数',
  n_dates: '日期数',
}

const PCT_METRIC_KEYS: Record<string, true> = {
  total_return: true,
  annual_return: true,
  benchmark_return: true,
  excess: true,
  max_drawdown: true,
  annual_volatility: true,
  downside_deviation: true,
  tracking_error: true,
  win_rate: true,
  avg_turnover: true,
  total_turnover: true,
  max_exposure: true,
  ic_win_rate: true,
}

const INT_METRIC_KEYS: Record<string, true> = {
  n_trades: true,
  pending_exit_positions: true,
  n_symbols: true,
  n_dates: true,
}

const DAYS_METRIC_KEYS: Record<string, true> = { avg_duration: true }

const SIGNED_METRIC_KEYS: Record<string, true> = {
  total_return: true,
  annual_return: true,
  benchmark_return: true,
  excess: true,
  alpha: true,
  sharpe: true,
  sortino: true,
  calmar: true,
  information_ratio: true,
}

const SNAPSHOT_LABELS: Record<string, string> = {
  canonical_generation: '规范数据代',
  canonical_start_date: '规范区间起点',
  canonical_end_date: '规范区间终点',
  local_overlay_latest_date: '本地补丁最新日期',
  data_start: '数据起点',
  data_cutoff: '数据截止',
  adjustment_mode: '复权方式',
  adjustment_generation: '复权代',
  source_generations: '数据源代',
  universe_definition: '股票池定义',
  universe_as_of: '股票池时点',
  snapshot_hash: '快照哈希',
}

/** 多空组合指标行：[key, 中文标签, 格式] */
const LONG_SHORT_ROWS: Array<[string, string, 'pct' | 'num']> = [
  ['total_return', '多空总收益', 'pct'],
  ['annual_return', '多空年化收益', 'pct'],
  ['max_drawdown', '多空最大回撤', 'pct'],
  ['annual_volatility', '多空年化波动', 'pct'],
  ['sharpe', '多空夏普', 'num'],
  ['sortino', '多空 Sortino', 'num'],
  ['calmar', '多空 Calmar', 'num'],
  ['omega', '多空 Omega', 'num'],
  ['tail_ratio', '多空尾部比率', 'num'],
  ['ulcer_index', '多空 Ulcer Index', 'num'],
  ['value_at_risk', '多空 VaR', 'num'],
  ['conditional_value_at_risk', '多空 CVaR', 'num'],
  ['downside_deviation', '多空下行波动', 'pct'],
  ['avg_turnover', '多空平均换手', 'pct'],
  ['total_turnover', '多空累计换手', 'pct'],
  ['total_cost', '多空总成本', 'pct'],
]

interface CurvePoint {
  date: string
  value: number
}

interface ChartSeriesDef {
  name: string
  color: string
  points: CurvePoint[]
}

type MetricFormat = 'pct' | 'num' | 'int' | 'days'

// ===== 基础工具 =====

/** 全量独立候选执行：退出事件日等权复利样本曲线，非账户净值 */
function isCandidateExecution(run: BacktestRun): boolean {
  return isPlainObject(run.stats) && run.stats.full_kind === 'candidate_execution'
}


/** HTML 转义；null/undefined 与非有限数值输出空字符串 */
export function escapeHtml(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return ''
    return String(value)
  }
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 提取有限数值：仅接受 number 与非空数字字符串，其余（含 null/undefined/布尔/对象）返回 null */
function finiteNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (trimmed === '') return null
    const parsed = Number(trimmed)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmptyText(value: unknown): string | null {
  if (typeof value === 'string' && value !== '') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return null
}

function formatUnknownValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '—'
  if (typeof value === 'string') return value === '' ? '—' : value
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return String(value)
  }
}

function formatPct(ratio: number | null): string {
  if (ratio === null) return '—'
  return `${(ratio * 100).toFixed(2)}%`
}

function formatDecimal(value: number | null, digits = 4): string {
  if (value === null) return '—'
  return value.toFixed(digits)
}

function formatInt(value: number | null): string {
  if (value === null) return '—'
  return String(Math.round(value))
}

function formatAxisValue(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 10000) return value.toFixed(0)
  if (abs >= 100) return value.toFixed(1)
  if (abs >= 1) return value.toFixed(2)
  if (abs >= 0.001) return value.toFixed(3)
  return value.toFixed(4)
}

// ===== 曲线提取与降采样 =====

/** 兼容旧 equity 键 / 新 value 键等多种字段名；过滤缺日期或非有限数值的点 */
function extractCurve(points: unknown, keys: string[]): CurvePoint[] {
  if (!Array.isArray(points)) return []
  const out: CurvePoint[] = []
  for (const raw of points) {
    if (!isPlainObject(raw)) continue
    const date = typeof raw.date === 'string' ? raw.date : ''
    if (date === '') continue
    let value: number | null = null
    for (const key of keys) {
      const candidate = finiteNumber(raw[key])
      if (candidate !== null) {
        value = candidate
        break
      }
    }
    if (value === null) continue
    out.push({ date, value })
  }
  return out
}

/** 均匀降采样到恰好 maxCount 个点（含首尾），不足则原样拷贝 */
export function downsample<T>(items: T[], maxCount: number): T[] {
  const total = items.length
  if (total <= maxCount) return items.slice()
  const out: T[] = []
  for (let i = 0; i < maxCount; i += 1) {
    out.push(items[Math.round((i * (total - 1)) / (maxCount - 1))])
  }
  return out
}

// ===== SVG 图表 =====

export function buildLineChartSvg(chartTitle: string, series: ChartSeriesDef[], height: number): string {
  const active = series.filter(item => item.points.length >= 2)
  if (active.length === 0) return ''
  const padLeft = 64
  const padRight = 14
  const padTop = 10
  const padBottom = 22
  const plotWidth = CHART_WIDTH - padLeft - padRight
  const plotHeight = height - padTop - padBottom

  const dates = new Set<string>()
  let vmin = Infinity
  let vmax = -Infinity
  for (const item of active) {
    for (const point of item.points) {
      dates.add(point.date)
      if (point.value < vmin) vmin = point.value
      if (point.value > vmax) vmax = point.value
    }
  }
  const sortedDates = Array.from(dates).sort()
  const dateIndex = new Map(sortedDates.map((date, index) => [date, index]))
  const span = Math.max(sortedDates.length - 1, 1)
  if (vmin === vmax) {
    vmin -= 1
    vmax += 1
  }
  const padding = (vmax - vmin) * 0.05
  vmin -= padding
  vmax += padding
  const xOf = (date: string) => padLeft + ((dateIndex.get(date) ?? 0) / span) * plotWidth
  const yOf = (value: number) => padTop + (1 - (value - vmin) / (vmax - vmin)) * plotHeight

  const parts: string[] = []
  parts.push(
    `<svg viewBox="0 0 ${CHART_WIDTH} ${height}" role="img" aria-label="${escapeHtml(chartTitle)}" preserveAspectRatio="xMidYMid meet">`
  )
  for (let i = 0; i <= 4; i += 1) {
    const value = vmin + ((vmax - vmin) * i) / 4
    const y = yOf(value)
    parts.push(
      `<line x1="${padLeft}" y1="${y.toFixed(1)}" x2="${CHART_WIDTH - padRight}" y2="${y.toFixed(1)}" stroke="#e5e7eb" stroke-width="1"/>`
    )
    parts.push(
      `<text x="${padLeft - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end" font-size="10" fill="#6b7280">${escapeHtml(formatAxisValue(value))}</text>`
    )
  }
  const labelIndexes = Array.from(
    new Set([0, Math.floor((sortedDates.length - 1) / 2), sortedDates.length - 1])
  )
  for (const index of labelIndexes) {
    const x = padLeft + (index / span) * plotWidth
    parts.push(
      `<text x="${x.toFixed(1)}" y="${height - 6}" text-anchor="middle" font-size="10" fill="#6b7280">${escapeHtml(sortedDates[index])}</text>`
    )
  }
  for (const item of active) {
    const coords = item.points
      .map(point => `${xOf(point.date).toFixed(1)},${yOf(point.value).toFixed(1)}`)
      .join(' ')
    parts.push(`<polyline fill="none" stroke="${item.color}" stroke-width="1.6" points="${coords}"/>`)
  }
  parts.push('</svg>')
  return parts.join('')
}

function renderLegend(items: Array<{ name: string; color: string }>): string {
  const spans = items
    .map(
      item =>
        `<span class="legend-item"><span class="sw" style="background:${item.color}"></span>${escapeHtml(item.name)}</span>`
    )
    .join('')
  return `<p class="legend">${spans}</p>`
}

// ===== 区块渲染 =====

function reportTitle(run: BacktestRun): string {
  const name =
    nonEmptyText(run.label) ?? nonEmptyText(run.subject?.name) ?? nonEmptyText(run.run_id) ?? '回测'
  return `回测报告 · ${name}`
}

function renderHeader(run: BacktestRun): string {
  const kindLabel = KIND_LABELS[run.kind] ?? '回测'
  const status = nonEmptyText(run.status) ?? '—'
  return [
    '<header>',
    `<h1>${escapeHtml(reportTitle(run))}</h1>`,
    `<p class="badges"><span class="badge">${escapeHtml(kindLabel)}</span><span class="badge badge-status">${escapeHtml(status)}</span></p>`,
    '</header>',
  ].join('')
}

function renderMetadataSection(run: BacktestRun): string {
  const rows: Array<[string, string]> = [
    ['运行 ID', nonEmptyText(run.run_id) ?? '—'],
    ['类型', KIND_LABELS[run.kind] ?? String(run.kind ?? '—')],
    ['状态', nonEmptyText(run.status) ?? '—'],
    ['创建时间', nonEmptyText(run.created_at) ?? '—'],
    ['标的名称', nonEmptyText(run.subject?.name) ?? nonEmptyText(run.subject?.id) ?? '—'],
    ['标的哈希', nonEmptyText(run.subject?.hash) ?? '—'],
    ['引擎版本', nonEmptyText(run.engine_version) ?? '—'],
    ['随机种子', formatInt(finiteNumber(run.random_seed))],
    ['基准', benchmarkDisplayName(run)],
    ['标签', nonEmptyText(run.label) ?? '—'],
    ['来源运行', nonEmptyText(run.source_run_id) ?? '—'],
  ]
  const body = rows
    .map(([key, value]) => `<tr><td class="k">${escapeHtml(key)}</td><td>${escapeHtml(value)}</td></tr>`)
    .join('')
  return `<section id="metadata"><h2>元信息</h2><table class="kv"><tbody>${body}</tbody></table></section>`
}

function renderRecordSection(
  sectionId: string,
  title: string,
  emptyText: string,
  record: Record<string, unknown> | null,
  labels: Record<string, string>
): string {
  let body: string
  if (record && Object.keys(record).length > 0) {
    const rows = Object.entries(record)
      .map(([key, value]) => {
        const label = escapeHtml(labels[key] ?? key)
        return `<tr><td class="k">${label}</td><td>${escapeHtml(formatUnknownValue(value))}</td></tr>`
      })
      .join('')
    body = `<table class="kv"><tbody>${rows}</tbody></table>`
  } else {
    body = `<p class="placeholder">${escapeHtml(emptyText)}</p>`
  }
  return `<section id="${sectionId}"><h2>${escapeHtml(title)}</h2>${body}</section>`
}

function renderConfigSection(run: BacktestRun): string {
  return renderRecordSection('config', '回测配置', '未记录配置信息。', isPlainObject(run.config) ? run.config : null, {})
}

function renderSnapshotSection(run: BacktestRun): string {
  return renderRecordSection(
    'snapshot',
    '数据快照',
    '未记录数据快照。',
    isPlainObject(run.data_snapshot) ? run.data_snapshot : null,
    SNAPSHOT_LABELS
  )
}

function metricToneClass(key: string, value: number): string {
  if (SIGNED_METRIC_KEYS[key]) return value > 0 ? 'pos' : value < 0 ? 'neg' : ''
  if (key === 'max_drawdown') return value < 0 ? 'neg' : ''
  return ''
}

function classifyMetricFormat(key: string): MetricFormat {
  if (PCT_METRIC_KEYS[key]) return 'pct'
  if (INT_METRIC_KEYS[key]) return 'int'
  if (DAYS_METRIC_KEYS[key]) return 'days'
  return 'num'
}

function metricRow(label: string, key: string, value: number, format: MetricFormat): string {
  let text: string
  if (format === 'pct') text = formatPct(value)
  else if (format === 'int') text = formatInt(value)
  else if (format === 'days') text = `${value.toFixed(1)} 天`
  else text = formatDecimal(value)
  const tone = metricToneClass(key, value)
  const cls = tone === '' ? 'num' : `num ${tone}`
  return `<tr><td>${escapeHtml(label)}</td><td class="${cls}">${escapeHtml(text)}</td></tr>`
}

/** stats 数值表：过滤非有限值与布尔，已知指标按固定顺序在前，未知键按字典序在后 */
function renderStatsTable(stats: Record<string, unknown>, candidate = false): string {
  const entries = Object.entries(stats).filter(([, value]) => finiteNumber(value) !== null)
  if (entries.length === 0) return '<p class="placeholder">未记录指标。</p>'
  const knownOrder = Object.keys(METRIC_LABELS)
  entries.sort((a, b) => {
    const ia = knownOrder.indexOf(a[0])
    const ib = knownOrder.indexOf(b[0])
    const ka = ia === -1 ? knownOrder.length : ia
    const kb = ib === -1 ? knownOrder.length : ib
    return ka !== kb ? ka - kb : a[0].localeCompare(b[0])
  })
  const rows = entries
    .map(([key, value]) => {
      let label = METRIC_LABELS[key] ?? key
      if (candidate) {
        if (key === 'total_return') label = '样本曲线累计'
        else if (key === 'max_drawdown') label = '样本曲线回撤'
      }
      return metricRow(label, key, finiteNumber(value) as number, classifyMetricFormat(key))
    })
    .join('')
  return `<table class="kv metrics"><tbody>${rows}</tbody></table>`
}



function renderLongShortTable(longShortStats: Record<string, unknown>): string {
  const rows = LONG_SHORT_ROWS.map(([key, label, format]) => {
    const value = finiteNumber(longShortStats[key])
    if (value === null) return ''
    return metricRow(label, key, value, format)
  })
    .filter(row => row !== '')
    .join('')
  if (rows === '') return ''
  return `<h3>多空组合</h3><table class="kv metrics"><tbody>${rows}</tbody></table>`
}

/** 因子 run 的 stats 可能缺 IC 键，回退到 factor_result 同名字段 */
function factorStatsForDisplay(
  stats: unknown,
  factorResult: Record<string, unknown> | null
): Record<string, unknown> {
  const merged = isPlainObject(stats) ? { ...stats } : {}
  if (factorResult) {
    for (const key of ['ic_mean', 'ic_std', 'ir', 'ic_win_rate', 'n_symbols', 'n_dates']) {
      if (finiteNumber(merged[key]) === null && finiteNumber(factorResult[key]) !== null) {
        merged[key] = factorResult[key]
      }
    }
  }
  return merged
}

function renderMetricsSection(run: BacktestRun): string {
  const factorResult = isPlainObject(run.factor_result) ? run.factor_result : null
  const candidate = isCandidateExecution(run)
  let body: string
  if (run.kind === 'factor') {
    body = renderStatsTable(factorStatsForDisplay(run.stats, factorResult), false)
    const longShortStats =
      factorResult !== null && isPlainObject(factorResult.long_short_stats)
        ? factorResult.long_short_stats
        : null
    if (longShortStats) body += renderLongShortTable(longShortStats)
  } else {
    body = renderStatsTable(isPlainObject(run.stats) ? run.stats : {}, candidate)
  }
  return `<section id="metrics"><h2>核心指标</h2>${body}</section>`
}


function renderWarningsSection(run: BacktestRun): string {
  const warnings = (Array.isArray(run.warnings) ? run.warnings : [])
    .map(item => (typeof item === 'string' ? item : String(item ?? '')))
    .filter(text => text !== '')
  const body =
    warnings.length === 0
      ? '<p class="placeholder">无警告。</p>'
      : `<ul class="warn">${warnings.map(text => `<li>${escapeHtml(text)}</li>`).join('')}</ul>`
  return `<section id="warnings"><h2>警告</h2>${body}</section>`
}

function renderAttributionSection(run: BacktestRun): string {
  const attribution = isPlainObject(run.attribution) ? run.attribution : null
  if (attribution === null) return ''

  const status = nonEmptyText(attribution.status) ?? 'unavailable'
  const scope = nonEmptyText(attribution.scope) ?? '未记录归因口径。'
  const classificationNote = nonEmptyText(attribution.classification_note)
  const reason = nonEmptyText(attribution.reason)
  const warnings = Array.isArray(attribution.warnings)
    ? attribution.warnings.map(item => nonEmptyText(item)).filter((item): item is string => item !== null)
    : []
  const brinson = isPlainObject(attribution.brinson) ? attribution.brinson : null
  const metricRows: Array<[string, unknown]> = brinson === null ? [] : [
    ['价值加权交易样本', brinson.portfolio_return],
    ['等权交易样本', brinson.benchmark_return],
    ['相对差异', brinson.excess_return],
    ['配置效应', brinson.allocation],
    ['选股效应', brinson.selection],
    ['交互效应', brinson.interaction],
  ]
  const metrics = metricRows
    .map(([label, value]) => {
      const number = finiteNumber(value)
      return number === null ? '' : metricRow(label, 'excess', number, 'pct')
    })
    .filter(Boolean)
    .join('')
  const groups = brinson !== null && Array.isArray(brinson.groups)
    ? brinson.groups.filter(isPlainObject)
    : []
  const groupRows = groups.map(group => {
    const cells = [
      `<td>${escapeHtml(nonEmptyOrDash(group.group))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.portfolio_weight)))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.benchmark_weight)))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.portfolio_return)))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.benchmark_return)))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.allocation)))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.selection)))}</td>`,
      `<td class="num">${escapeHtml(formatPct(finiteNumber(group.interaction)))}</td>`,
    ]
    return `<tr>${cells.join('')}</tr>`
  }).join('')
  const groupTable = groupRows === ''
    ? '<p class="placeholder">无有效行业分组。</p>'
    : `<table class="grid"><thead><tr><th>行业</th><th class="num">组合权重</th><th class="num">等权基准</th><th class="num">组合收益</th><th class="num">等权收益</th><th class="num">配置</th><th class="num">选股</th><th class="num">交互</th></tr></thead><tbody>${groupRows}</tbody></table>`
  const body = status === 'ok' && brinson !== null
    ? `${metrics === '' ? '' : `<table class="kv metrics"><tbody>${metrics}</tbody></table>`}${groupTable}`
    : `<p class="placeholder">${escapeHtml(reason ?? '归因所需数据不足，未生成数值。')}</p>`
  const famaFrench = isPlainObject(attribution.fama_french) ? attribution.fama_french : null
  const famaDetail = famaFrench === null
    ? 'Fama-French：未记录。'
    : `Fama-French：${nonEmptyText(famaFrench.detail) ?? nonEmptyText(famaFrench.reason) ?? '不可用。'}`
  const warningBlock = warnings.length === 0
    ? ''
    : `<ul class="warn">${warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join('')}</ul>`
  const coverage = formatPct(finiteNumber(attribution.capital_coverage))

  return `<section id="attribution"><h2>交易窗口行业归因</h2><p class="note">${escapeHtml(scope)}</p>${classificationNote === null ? '' : `<p class="note">${escapeHtml(classificationNote)}</p>`}<p class="note">已归类 ${escapeHtml(formatInt(finiteNumber(attribution.classified_trades)))} / ${escapeHtml(formatInt(finiteNumber(attribution.input_trades)))} 笔；资金覆盖 ${escapeHtml(coverage)}。</p>${body}<p class="note">${escapeHtml(famaDetail)}</p>${warningBlock}</section>`
}

function benchmarkDisplayName(run: BacktestRun): string {
  const meta = isPlainObject(run.benchmark) ? run.benchmark : null
  const fromMeta = nonEmptyText(meta?.name) ?? nonEmptyText(meta?.symbol)
  const firstRow =
    Array.isArray(run.benchmark_curve) && isPlainObject(run.benchmark_curve[0])
      ? run.benchmark_curve[0]
      : null
  const fromRow = nonEmptyText(firstRow?.name) ?? nonEmptyText(firstRow?.symbol)
  const name = fromMeta ?? fromRow
  return name === null ? '基准' : `基准 · ${name}`
}

function renderCurvesSection(run: BacktestRun): string {
  const factorResult = isPlainObject(run.factor_result) ? run.factor_result : null
  const equity = extractCurve(run.equity_curve, ['value', 'equity'])
  const benchmark = extractCurve(run.benchmark_curve, ['value', 'close'])
  const drawdown = extractCurve(run.drawdown_curve, ['value'])
  const longShort = extractCurve(factorResult?.long_short_nav, ['value'])
  const blocks: string[] = []
  const candidate = isCandidateExecution(run)

  const factorMode = run.kind === 'factor' || (equity.length < 2 && longShort.length >= 2)
  if (factorMode) {
    if (longShort.length >= 2) {
      const series: ChartSeriesDef[] = [
        { name: '多空净值', color: COLOR_LONG_SHORT, points: downsample(longShort, MAX_CURVE_POINTS) },
      ]
      blocks.push(renderLegend([{ name: '多空净值', color: COLOR_LONG_SHORT }]))
      blocks.push(buildLineChartSvg('多空净值曲线', series, EQUITY_CHART_HEIGHT))
    } else {
      blocks.push('<p class="placeholder">多空净值曲线数据不足，无法绘图。</p>')
    }
  } else if (candidate) {
    // 候选样本曲线：退出事件日等权复利，非账户净值；不绘制基准
    if (equity.length >= 2) {
      const series: ChartSeriesDef[] = [
        {
          name: '候选样本收益',
          color: COLOR_EQUITY,
          points: downsample(equity, MAX_CURVE_POINTS),
        },
      ]
      blocks.push(
        '<p class="note">按退出事件日等权复利，非可交易账户净值。</p>'
      )
      blocks.push(renderLegend([{ name: '候选样本收益', color: COLOR_EQUITY }]))
      blocks.push(buildLineChartSvg('候选样本收益曲线', series, EQUITY_CHART_HEIGHT))
    } else {
      blocks.push('<p class="placeholder">候选样本收益曲线数据不足，无法绘图。</p>')
    }
    if (drawdown.length >= 2) {
      const series: ChartSeriesDef[] = [
        { name: '样本回撤', color: COLOR_DRAWDOWN, points: downsample(drawdown, MAX_CURVE_POINTS) },
      ]
      blocks.push(renderLegend([{ name: '样本回撤', color: COLOR_DRAWDOWN }]))
      blocks.push(buildLineChartSvg('样本回撤曲线', series, DRAWDOWN_CHART_HEIGHT))
    }
  } else {
    const mainSeries: ChartSeriesDef[] = []
    if (equity.length >= 2) {
      mainSeries.push({
        name: '策略净值',
        color: COLOR_EQUITY,
        points: downsample(equity, MAX_CURVE_POINTS),
      })
    }
    if (benchmark.length >= 2) {
      mainSeries.push({
        name: benchmarkDisplayName(run),
        color: COLOR_BENCHMARK,
        points: downsample(benchmark, MAX_CURVE_POINTS),
      })
    }
    if (mainSeries.length > 0) {
      blocks.push(renderLegend(mainSeries.map(item => ({ name: item.name, color: item.color }))))
      blocks.push(buildLineChartSvg('净值与基准曲线', mainSeries, EQUITY_CHART_HEIGHT))
    } else {
      blocks.push('<p class="placeholder">净值曲线数据不足，无法绘图。</p>')
    }
    if (drawdown.length >= 2) {
      const series: ChartSeriesDef[] = [
        { name: '回撤', color: COLOR_DRAWDOWN, points: downsample(drawdown, MAX_CURVE_POINTS) },
      ]
      blocks.push(renderLegend([{ name: '回撤', color: COLOR_DRAWDOWN }]))
      blocks.push(buildLineChartSvg('回撤曲线', series, DRAWDOWN_CHART_HEIGHT))
    }
  }
  return `<section id="curves"><h2>曲线</h2>${blocks.join('')}</section>`
}


function nonEmptyOrDash(value: unknown): string {
  return nonEmptyText(value) ?? '—'
}

function renderTradeRow(trade: Record<string, unknown>): string {
  const pnl = finiteNumber(trade.pnl_pct)
  const pnlText = pnl === null ? '—' : formatPct(pnl)
  const pnlClass = pnl === null ? 'num' : pnl >= 0 ? 'num pos' : 'num neg'
  const cells = [
    `<td>${escapeHtml(nonEmptyOrDash(trade.symbol))}</td>`,
    `<td>${escapeHtml(nonEmptyOrDash(trade.name))}</td>`,
    `<td>${escapeHtml(nonEmptyOrDash(trade.entry_date))}</td>`,
    `<td>${escapeHtml(nonEmptyOrDash(trade.exit_date))}</td>`,
    `<td class="num">${escapeHtml(formatDecimal(finiteNumber(trade.entry_price), 2))}</td>`,
    `<td class="num">${escapeHtml(formatDecimal(finiteNumber(trade.exit_price), 2))}</td>`,
    `<td class="${pnlClass}">${escapeHtml(pnlText)}</td>`,
    `<td class="num">${escapeHtml(formatInt(finiteNumber(trade.duration)))}</td>`,
    `<td>${escapeHtml(nonEmptyOrDash(trade.exit_reason))}</td>`,
  ]
  return `<tr class="trade">${cells.join('')}</tr>`
}

function renderTradesSection(run: BacktestRun): string {
  const trades = (Array.isArray(run.trades) ? run.trades : []).filter(row => isPlainObject(row))
  let body: string
  if (trades.length === 0) {
    body = '<p class="placeholder">无交易明细。</p>'
  } else {
    const shown = trades.slice(0, MAX_TRADE_ROWS)
    const note =
      trades.length > MAX_TRADE_ROWS
        ? `<p class="note">共 ${trades.length} 笔交易，仅展示前 ${MAX_TRADE_ROWS} 笔。</p>`
        : ''
    const rows = shown.map(row => renderTradeRow(row as Record<string, unknown>)).join('')
    body = `${note}<table class="grid"><thead><tr><th>代码</th><th>名称</th><th>入场日</th><th>出场日</th><th class="num">入场价</th><th class="num">出场价</th><th class="num">收益率</th><th class="num">持有天数</th><th>退出原因</th></tr></thead><tbody>${rows}</tbody></table>`
  }
  return `<section id="detail"><h2>交易明细</h2>${body}</section>`
}

const GROUP_COLUMNS: Array<{ key: string; label: string; format: 'pct' | 'num' | 'int' | 'text' }> = [
  { key: 'group', label: '分组', format: 'int' },
  { key: 'label', label: '标签', format: 'text' },
  { key: 'total_return', label: '总收益', format: 'pct' },
  { key: 'annual_return', label: '年化收益', format: 'pct' },
  { key: 'max_drawdown', label: '最大回撤', format: 'pct' },
  { key: 'sharpe', label: '夏普', format: 'num' },
  { key: 'win_rate', label: '胜率', format: 'pct' },
  { key: 'avg_turnover', label: '平均换手', format: 'pct' },
  { key: 'total_turnover', label: '累计换手', format: 'pct' },
  { key: 'total_cost', label: '总成本', format: 'pct' },
]

function renderGroupCell(value: unknown, format: 'pct' | 'num' | 'int' | 'text'): string {
  if (format === 'text') return `<td>${escapeHtml(nonEmptyOrDash(value))}</td>`
  const num = finiteNumber(value)
  if (num === null) return '<td class="num">—</td>'
  const text =
    format === 'pct' ? formatPct(num) : format === 'int' ? formatInt(num) : formatDecimal(num)
  return `<td class="num">${escapeHtml(text)}</td>`
}

function renderGroupStatsSection(groupStats: Array<Record<string, unknown>>): string {
  if (groupStats.length === 0) {
    return `<section id="detail"><h2>分组统计</h2><p class="placeholder">无分组统计。</p></section>`
  }
  const head = GROUP_COLUMNS.map(col =>
    col.format === 'text' ? `<th>${escapeHtml(col.label)}</th>` : `<th class="num">${escapeHtml(col.label)}</th>`
  ).join('')
  const rows = groupStats
    .map(row => `<tr class="group">${GROUP_COLUMNS.map(col => renderGroupCell(row[col.key], col.format)).join('')}</tr>`)
    .join('')
  return `<section id="detail"><h2>分组统计</h2><table class="grid"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></section>`
}

function renderDetailSection(run: BacktestRun): string {
  const factorResult = isPlainObject(run.factor_result) ? run.factor_result : null
  const trades = Array.isArray(run.trades) ? run.trades : []
  const groupStats =
    Array.isArray(factorResult?.group_stats) && factorResult !== null
      ? (factorResult.group_stats as unknown[]).filter(row => isPlainObject(row)).map(row => row as Record<string, unknown>)
      : []
  const factorMode = run.kind === 'factor' || (trades.length === 0 && groupStats.length > 0)
  if (factorMode) return renderGroupStatsSection(groupStats)
  return renderTradesSection(run)
}

// ===== 主入口 =====

const REPORT_CSS = [
  '* { box-sizing: border-box; }',
  'body { margin: 24px auto; max-width: 960px; padding: 0 20px; color: #111827; background: #ffffff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; font-size: 14px; line-height: 1.6; }',
  'header { margin-bottom: 8px; }',
  'h1 { margin: 0 0 8px; font-size: 22px; }',
  'h2 { margin: 0 0 10px; padding-left: 9px; border-left: 3px solid #2563eb; font-size: 15px; }',
  'h3 { margin: 14px 0 6px; font-size: 13px; color: #374151; }',
  'section { margin: 22px 0; }',
  'table { width: 100%; border-collapse: collapse; font-size: 12.5px; }',
  'th, td { border: 1px solid #e5e7eb; padding: 4px 8px; text-align: left; vertical-align: top; }',
  'th { background: #f9fafb; font-weight: 600; white-space: nowrap; }',
  'td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }',
  'table.kv td:first-child { width: 180px; color: #4b5563; background: #f9fafb; white-space: nowrap; }',
  '.badges { margin: 0 0 4px; }',
  '.badge { display: inline-block; margin-right: 6px; padding: 1px 10px; border-radius: 999px; background: #eef2ff; color: #3730a3; font-size: 12px; }',
  '.badge-status { background: #ecfdf5; color: #065f46; }',
  '.legend { margin: 6px 0; font-size: 12px; color: #374151; }',
  '.legend-item { margin-right: 14px; white-space: nowrap; }',
  '.sw { display: inline-block; width: 12px; height: 3px; margin-right: 4px; vertical-align: middle; border-radius: 1px; }',
  'svg { display: block; width: 100%; height: auto; }',
  'ul.warn { margin: 0; padding-left: 18px; color: #92400e; }',
  'ul.warn li { margin: 2px 0; }',
  '.placeholder { margin: 0; color: #9ca3af; font-size: 12.5px; }',
  '.note { margin: 0 0 6px; color: #6b7280; font-size: 12px; }',
  '.pos { color: #15803d; }',
  '.neg { color: #b91c1c; }',
  'footer { margin-top: 36px; padding-top: 10px; border-top: 1px solid #e5e7eb; color: #9ca3af; font-size: 11px; }',
  '@media print {',
  '  @page { margin: 12mm; }',
  '  body { margin: 0; max-width: none; padding: 0; }',
  '  section { break-inside: avoid; page-break-inside: avoid; }',
  '  table { font-size: 11px; }',
  '}',
].join('\n')

/**
 * 生成完整自包含 HTML 回测报告。
 * - 策略 run：净值/基准/回撤内联 SVG（每条曲线 ≤240 点）+ 交易明细（≤200 行）
 * - 因子 run：多空净值 SVG + 分组统计表
 * - 兼容旧 equity 键曲线；缺字段/无效曲线安全降级为占位文案
 */
export function buildRunReportHtml(run: BacktestRun): string {
  const parts: string[] = []
  parts.push('<!DOCTYPE html>')
  parts.push('<html lang="zh-CN">')
  parts.push('<head>')
  parts.push('<meta charset="utf-8">')
  parts.push('<meta name="viewport" content="width=device-width, initial-scale=1">')
  parts.push(`<title>${escapeHtml(reportTitle(run))}</title>`)
  parts.push(`<style>${REPORT_CSS}</style>`)
  parts.push('</head>')
  parts.push('<body>')
  parts.push(renderHeader(run))
  parts.push(renderMetadataSection(run))
  parts.push(renderConfigSection(run))
  parts.push(renderSnapshotSection(run))
  parts.push(renderMetricsSection(run))
  parts.push(renderWarningsSection(run))
  parts.push(renderAttributionSection(run))
  parts.push(renderCurvesSection(run))
  parts.push(renderDetailSection(run))
  parts.push('<footer>离线回测报告 · 自包含 HTML，可直接打印或存档</footer>')
  parts.push('</body>')
  parts.push('</html>')
  return parts.join('\n')
}
