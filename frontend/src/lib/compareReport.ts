import type {
  BacktestRunComparison,
  BacktestRunConfigDiff,
  BacktestRunKind,
  BacktestRunSummary,
  BacktestRunTradeCommonSample,
  BacktestRunTradeSample,
  BacktestRunTradeSummary,
} from './api.ts'
import { buildLineChartSvg, downsample, escapeHtml } from './backtestReport.ts'
import { fmtPct } from './format.ts'

/**
 * Run 对比共享口径 + 离线对比报告生成器（纯函数、零运行时依赖、无 DOM）。
 * - 共享口径区（调色板/指标元数据/格式化函数）由 RunHistoryPanel 与本生成器共用，
 *   保证对比视图与导出报告的标签、数值格式、Δ 口径完全一致。
 * - 报告为自包含 HTML：内联样式、纯 SVG polyline 净值曲线（不引 ECharts）、中文字体回退。
 */

// ===== 共享口径（RunHistoryPanel 与报告共用） =====

/** 对比调色板（8 色循环，覆盖 MAX_COMPARE=8）— 字面量约定：canvas/SVG 均无法消费 CSS 变量 */
export const COMPARE_COLORS = [
  '#3b82f6', '#f59e0b', '#14b8a6', '#ef4444',
  '#8b5cf6', '#ec4899', '#84cc16', '#64748b',
]

export const KIND_LABELS: Record<BacktestRunKind, string> = {
  strategy: '策略',
  factor: '因子',
  composite: '组合',
}

/** 常见指标中文名与格式；pct 为小数比率(0.12=12%)，int 取整，number 自适应精度 */
export const METRIC_META: Record<string, { label: string; format: 'pct' | 'number' | 'int' }> = {
  total_return: { label: '累计收益', format: 'pct' },
  annual_return: { label: '年化收益', format: 'pct' },
  benchmark_return: { label: '基准收益', format: 'pct' },
  excess: { label: '超额收益', format: 'pct' },
  max_drawdown: { label: '最大回撤', format: 'pct' },
  win_rate: { label: '胜率', format: 'pct' },
  median_return: { label: '收益中位数', format: 'pct' },
  volatility: { label: '年化波动', format: 'pct' },
  annual_volatility: { label: '年化波动', format: 'pct' },
  sharpe: { label: '夏普比率', format: 'number' },
  calmar: { label: '卡玛比率', format: 'number' },
  profit_factor: { label: '利润因子', format: 'number' },
  recovery_factor: { label: '恢复因子', format: 'number' },
  payoff_ratio: { label: '盈亏比', format: 'number' },
  ic_mean: { label: 'IC 均值', format: 'number' },
  ic_std: { label: 'IC 标准差', format: 'number' },
  ir: { label: 'IR', format: 'number' },
  ic_win_rate: { label: 'IC 胜率', format: 'pct' },
  n_trades: { label: '交易数', format: 'int' },
  n_symbols: { label: '标的数', format: 'int' },
  n_dates: { label: '交易日数', format: 'int' },
  avg_duration: { label: '平均持仓天数', format: 'number' },
  sortino: { label: 'Sortino', format: 'number' },
  omega: { label: 'Omega', format: 'number' },
  tail_ratio: { label: '尾部比率', format: 'number' },
  ulcer_index: { label: 'Ulcer Index', format: 'pct' },
  downside_deviation: { label: '下行偏差', format: 'pct' },
  value_at_risk: { label: 'VaR (5%)', format: 'pct' },
  conditional_value_at_risk: { label: 'CVaR (5%)', format: 'pct' },
  alpha: { label: 'Alpha', format: 'pct' },
  beta: { label: 'Beta', format: 'number' },
  tracking_error: { label: '跟踪误差', format: 'pct' },
  information_ratio: { label: '信息比率', format: 'number' },
  benchmark_correlation: { label: '基准相关性', format: 'number' },
  total_cost: { label: '总成本', format: 'pct' },
  avg_turnover: { label: '平均换手', format: 'pct' },
  max_exposure: { label: '最大敞口', format: 'pct' },
  total_turnover: { label: '累计换手', format: 'pct' },
}

/** 矩阵/详情中优先展示的核心指标顺序（与后端 HEADLINE_METRICS + 因子头部对齐） */
export const CORE_METRIC_ORDER = [
  'total_return',
  'annual_return',
  'benchmark_return',
  'excess',
  'sharpe',
  'max_drawdown',
  'win_rate',
  'profit_factor',
  'ic_mean',
  'ir',
  'sortino',
  'calmar',
  'annual_volatility',
  'recovery_factor',
]

export function metricLabel(key: string): string {
  return METRIC_META[key]?.label ?? key
}

export function formatMetricValue(key: string, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const meta = METRIC_META[key]
  if (meta?.format === 'pct') return fmtPct(value)
  if (meta?.format === 'int') return String(Math.round(value))
  // IC 等小数量级指标保留 4 位，其余 2 位
  return Math.abs(value) < 0.1 && value !== 0 ? value.toFixed(4) : value.toFixed(2)
}

/** 格式化差值：百分比指标由 fmtPct 自带正号，数值指标手动加正号 */
export function formatDeltaValue(key: string, delta: number): string {
  const meta = METRIC_META[key]
  if (meta?.format === 'pct') {
    // fmtPct 已自行添加 + 前缀，直接返回
    return formatMetricValue(key, delta)
  }
  const formatted = formatMetricValue(key, delta)
  return delta > 0 ? `+${formatted}` : formatted
}

export function fmtDateTime(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function runDisplayName(run: Pick<BacktestRunSummary, 'label' | 'subject' | 'run_id'>): string {
  return run.label || run.subject.name || run.run_id
}

/** compare.* 警告去掉机器前缀，直接展示后端中文说明 */
export function compareWarningLabel(warning: string): string {
  if (!warning.startsWith('compare.')) return warning
  const idx = warning.indexOf(':')
  return idx >= 0 ? warning.slice(idx + 1).trim() : warning
}

/** diff 值的紧凑展示: 标量直出, 结构 JSON 化并截断 */
export function formatDiffValue(raw: unknown): string {
  if (raw == null) return '—'
  if (typeof raw === 'string') return raw === '' ? '""' : raw
  if (typeof raw === 'number') return Number.isFinite(raw) ? String(raw) : '—'
  if (typeof raw === 'boolean') return raw ? 'true' : 'false'
  try {
    const text = JSON.stringify(raw)
    return text.length > 96 ? `${text.slice(0, 96)}…` : text
  } catch {
    return String(raw)
  }
}

/** 份额/金额展示: null/非有限 → '—' (旧 run 缺字段不伪装成 0) */
export function fmtNumOrDash(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return Math.abs(value) >= 1000 ? value.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : String(Math.round(value * 100) / 100)
}

export function fmtPctOrDash(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : fmtPct(value)
}

// ===== 报告专用 =====

/** 每条净值曲线降采样上限（与单 run 报告一致，8 条曲线亦不会超载） */
const MAX_CURVE_POINTS = 240

/** 报告内矩阵的最小宽度（px）：指标列 + 每列固定宽，列多时在 .table-wrap 内横向滚动而非挤压 */
const MATRIX_LABEL_WIDTH = 180
const MATRIX_COLUMN_WIDTH = 132

const CONFIG_DIFF_OP_META: Record<string, { label: string; cls: string }> = {
  added: { label: '新增', cls: 'op-add' },
  removed: { label: '移除', cls: 'op-del' },
  changed: { label: '修改', cls: 'op-chg' },
}

const COMPARE_REPORT_CSS = [
  '* { box-sizing: border-box; }',
  'body { margin: 24px auto; max-width: 1080px; padding: 0 20px; color: #111827; background: #ffffff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; font-size: 14px; line-height: 1.6; }',
  'header { margin-bottom: 8px; }',
  'h1 { margin: 0 0 8px; font-size: 22px; }',
  'h2 { margin: 0 0 10px; padding-left: 9px; border-left: 3px solid #2563eb; font-size: 15px; }',
  'section { margin: 22px 0; }',
  'table { width: 100%; border-collapse: collapse; font-size: 12.5px; }',
  'th, td { border: 1px solid #e5e7eb; padding: 4px 8px; text-align: left; vertical-align: top; }',
  'th { background: #f9fafb; font-weight: 600; white-space: nowrap; }',
  'td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }',
  '.table-wrap { overflow-x: auto; }',
  'table.kv td:first-child { width: 180px; color: #4b5563; background: #f9fafb; white-space: nowrap; }',
  '.run-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }',
  '.delta { display: block; margin-top: 1px; font-size: 10.5px; color: #6b7280; font-weight: 400; }',
  '.muted { color: #6b7280; font-weight: 400; }',
  '.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; }',
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
  '.op-add { color: #15803d; }',
  '.op-del { color: #b91c1c; }',
  '.op-chg { color: #b45309; }',
  '.cand { margin: 12px 0 16px; }',
  '.cand-head { margin: 4px 0 6px; font-size: 13px; }',
  '.row-diff td { background: #fffbeb; }',
  'footer { margin-top: 36px; padding-top: 10px; border-top: 1px solid #e5e7eb; color: #9ca3af; font-size: 11px; }',
  'footer p { margin: 2px 0; }',
  '@media print {',
  '  @page { margin: 12mm; }',
  '  body { margin: 0; max-width: none; padding: 0; }',
  '  section { break-inside: avoid; page-break-inside: avoid; }',
  '  table { font-size: 11px; }',
  '}',
].join('\n')

/** 报告内百分比指标的红绿着色（对应 UI 的 priceColorClass 口径：正绿负红） */
function metricToneClass(key: string, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return ''
  if (METRIC_META[key]?.format !== 'pct') return ''
  if (value > 0) return 'pos'
  if (value < 0) return 'neg'
  return ''
}

function candidateName(comparison: BacktestRunComparison, runId: string): string {
  const run = comparison.runs.find(item => item.run_id === runId)
  return escapeHtml(run ? runDisplayName(run) : runId.slice(0, 8))
}

// ===== 区块渲染 =====

function renderMetaSection(comparison: BacktestRunComparison, now: Date): string {
  const rows = comparison.runs
    .map((run, index) => {
      const color = COMPARE_COLORS[index % COMPARE_COLORS.length]
      const baselineTag = index === 0 ? ' <span class="muted">（基线）</span>' : ''
      return '<tr>' +
        `<td><span class="run-dot" style="background:${color}"></span></td>` +
        `<td>${escapeHtml(runDisplayName(run))}${baselineTag}</td>` +
        `<td>${escapeHtml(run.label || '—')}</td>` +
        `<td>${escapeHtml(KIND_LABELS[run.kind] ?? run.kind)}</td>` +
        `<td class="mono">${escapeHtml(run.run_id)}</td>` +
        `<td>${escapeHtml(fmtDateTime(run.created_at))}</td>` +
        `<td>${escapeHtml(run.start ?? '—')} ~ ${escapeHtml(run.end ?? '—')}</td>` +
        `<td class="num">${run.n_trades}</td>` +
        '</tr>'
    })
    .join('')
  return [
    '<section id="meta">',
    '<h2>对比概览</h2>',
    '<table class="kv">',
    `<tr><td>生成时间</td><td>${escapeHtml(fmtDateTime(now.toISOString()))}</td></tr>`,
    `<tr><td>运行数量</td><td>${comparison.runs.length} 个（首行为对比基线）</td></tr>`,
    '</table>',
    '<div class="table-wrap"><table>',
    '<thead><tr><th>#</th><th>名称</th><th>标签</th><th>类型</th><th>Run ID</th><th>创建时间</th><th>回测区间</th><th class="num">交易数</th></tr></thead>',
    `<tbody>${rows}</tbody>`,
    '</table></div>',
    '</section>',
  ].join('\n')
}

function renderWarningsSection(comparison: BacktestRunComparison): string {
  if (!comparison.warnings || comparison.warnings.length === 0) return ''
  const items = comparison.warnings.map(warning => `<li>${escapeHtml(compareWarningLabel(warning))}</li>`).join('')
  return [
    '<section id="warnings">',
    '<h2>可比性提醒</h2>',
    `<ul class="warn">${items}</ul>`,
    '</section>',
  ].join('\n')
}

function renderMatrixSection(comparison: BacktestRunComparison): string {
  const runOrder = comparison.runs.map(run => run.run_id)
  const metricKeys = Object.keys(comparison.metric_matrix ?? {})
  if (metricKeys.length === 0 || runOrder.length === 0) {
    return '<section id="matrix"><h2>指标矩阵</h2><p class="placeholder">后端未返回指标矩阵。</p></section>'
  }
  const coreKeys = CORE_METRIC_ORDER.filter(key => metricKeys.includes(key))
  const extraKeys = metricKeys.filter(key => !CORE_METRIC_ORDER.includes(key)).sort()
  const orderedKeys = [...coreKeys, ...extraKeys]

  const headerCells = comparison.runs
    .map((run, index) => {
      const color = COMPARE_COLORS[index % COMPARE_COLORS.length]
      const sub = `${run.run_id.slice(0, 8)} · ${KIND_LABELS[run.kind] ?? run.kind}`
      return `<th class="num"><span class="run-dot" style="background:${color}"></span>${escapeHtml(runDisplayName(run))}` +
        `<span class="delta">${escapeHtml(sub)}</span></th>`
    })
    .join('')

  const bodyRows = orderedKeys
    .map(key => {
      const cells = runOrder
        .map((runId, index) => {
          const value = comparison.metric_matrix[key]?.[runId] ?? null
          const baseline = comparison.metric_matrix[key]?.[runOrder[0]] ?? null
          const delta = index > 0 && Number.isFinite(value) && Number.isFinite(baseline)
            ? Number(value) - Number(baseline)
            : null
          const deltaText = index === 0 ? '基线' : delta == null ? 'Δ —' : `Δ ${formatDeltaValue(key, delta)}`
          return `<td class="num"><span class="${metricToneClass(key, value)}">${escapeHtml(formatMetricValue(key, value))}</span>` +
            `<span class="delta">${escapeHtml(deltaText)}</span></td>`
        })
        .join('')
      return `<tr><td class="mono">${escapeHtml(metricLabel(key))}</td>${cells}</tr>`
    })
    .join('')

  const minWidth = MATRIX_LABEL_WIDTH + comparison.runs.length * MATRIX_COLUMN_WIDTH
  return [
    '<section id="matrix">',
    '<h2>指标矩阵</h2>',
    '<p class="note">核心指标在前，其余按字典序排列；首列为对比基线，其余列同时给出相对基线的 Δ（Δ = 对比值 − 基线值）。</p>',
    `<div class="table-wrap"><table style="min-width:${minWidth}px">`,
    `<thead><tr><th>指标</th>${headerCells}</tr></thead>`,
    `<tbody>${bodyRows}</tbody>`,
    '</table></div>',
    '</section>',
  ].join('\n')
}

function renderConfigDiffSection(comparison: BacktestRunComparison): string {
  const diff: BacktestRunConfigDiff | undefined = comparison.config_diff
  if (!diff) {
    return '<section id="config-diff"><h2>配置差异</h2><p class="placeholder">后端响应未包含配置差异（旧版本对比接口）。</p></section>'
  }
  const baselineRun = comparison.runs.find(run => run.run_id === diff.baseline_run_id)
  const blocks = diff.candidates
    .map(candidate => {
      const name = candidateName(comparison, candidate.run_id)
      if (candidate.total === 0) {
        return `<div class="cand"><div class="cand-head">${name} <span class="muted">与基线配置一致，无差异项。</span></div></div>`
      }
      const rows = candidate.entries
        .map(entry => {
          const meta = CONFIG_DIFF_OP_META[entry.op]
          return '<tr>' +
            `<td class="mono">${escapeHtml(entry.path)}</td>` +
            `<td class="${meta?.cls ?? ''}">${escapeHtml(meta?.label ?? entry.op)}</td>` +
            `<td class="mono">${escapeHtml(formatDiffValue(entry.before))}</td>` +
            `<td class="mono">${escapeHtml(formatDiffValue(entry.after))}</td>` +
            '</tr>'
        })
        .join('')
      const truncated = candidate.truncated
        ? `<p class="note">差异较多，仅展示前 ${candidate.entries.length} 项（共 ${candidate.total} 项）。</p>`
        : ''
      return `<div class="cand">` +
        `<div class="cand-head">${name} <span class="muted">共 ${candidate.total} 项差异</span></div>` +
        '<div class="table-wrap"><table style="min-width:640px">' +
        '<thead><tr><th>配置项</th><th>变化</th><th>基线值</th><th>对比值</th></tr></thead>' +
        `<tbody>${rows}</tbody>` +
        '</table></div>' +
        truncated +
        '</div>'
    })
    .join('')
  return [
    '<section id="config-diff">',
    '<h2>配置差异</h2>',
    `<p class="note">基线：${escapeHtml(baselineRun ? runDisplayName(baselineRun) : diff.baseline_run_id.slice(0, 8))}</p>`,
    blocks,
    '</section>',
  ].join('\n')
}

function renderCommonTradeRow(row: BacktestRunTradeCommonSample): string {
  const cls = row.value_differs ? ' class="row-diff"' : ''
  return `<tr${cls}>` +
    `<td class="mono">${escapeHtml(row.symbol ?? '—')}</td>` +
    `<td>${escapeHtml(row.entry_date ?? '—')}</td>` +
    `<td>${escapeHtml(row.exit_date ?? '—')}</td>` +
    `<td class="num">${fmtNumOrDash(row.baseline.shares)}</td>` +
    `<td class="num">${fmtNumOrDash(row.candidate.shares)}</td>` +
    `<td class="num">${fmtNumOrDash(row.baseline.entry_value)}</td>` +
    `<td class="num">${fmtNumOrDash(row.candidate.entry_value)}</td>` +
    `<td class="num">${fmtNumOrDash(row.baseline.exit_value)}</td>` +
    `<td class="num">${fmtNumOrDash(row.candidate.exit_value)}</td>` +
    `<td class="num">${fmtPctOrDash(row.baseline.pnl_pct)}</td>` +
    `<td class="num">${fmtPctOrDash(row.candidate.pnl_pct)}</td>` +
    '</tr>'
}

function renderTradeSampleRow(row: BacktestRunTradeSample): string {
  return '<tr>' +
    `<td class="mono">${escapeHtml(row.symbol ?? '—')}</td>` +
    `<td>${escapeHtml(row.entry_date ?? '—')}</td>` +
    `<td>${escapeHtml(row.exit_date ?? '—')}</td>` +
    `<td class="num">${fmtNumOrDash(row.shares)}</td>` +
    `<td class="num">${fmtNumOrDash(row.entry_value)}</td>` +
    `<td class="num">${fmtPctOrDash(row.pnl_pct)}</td>` +
    '</tr>'
}

function renderTradeSection(comparison: BacktestRunComparison): string {
  const summary: BacktestRunTradeSummary | undefined = comparison.trade_summary
  const sectionHead = '<section id="trade-change"><h2>交易变动摘要</h2>'
  if (!summary) {
    return `${sectionHead}<p class="placeholder">后端响应未包含交易变动摘要（旧版本对比接口）。</p></section>`
  }
  const baselineRun = comparison.runs.find(run => run.run_id === summary.baseline_run_id)
  const baselineNote = `基线 ${escapeHtml(baselineRun ? runDisplayName(baselineRun) : summary.baseline_run_id.slice(0, 8))} · ${summary.baseline_n_trades} 笔 · 共同 = 相同(标的, 入场日, 出场日)`
  const noTrades = summary.baseline_n_trades === 0 && summary.candidates.every(c => c.n_trades === 0)
  if (noTrades) {
    return `${sectionHead}<p class="note">${baselineNote}</p>` +
      '<p class="placeholder">所选 run 均无交易明细（因子 run / 旧记录），无可比较的交易变化。</p></section>'
  }

  const blocks = summary.candidates
    .map(candidate => {
      const name = candidateName(comparison, candidate.run_id)
      const counts = `<span class="muted">共同 ${candidate.common}` +
        (candidate.common_value_diff > 0 ? ` · 份额/金额不同 ${candidate.common_value_diff}` : '') +
        ` · <span class="pos">新增 ${candidate.added}</span>` +
        ` · <span class="neg">消失 ${candidate.removed}</span></span>`
      const commonTable = candidate.samples.common.length > 0
        ? '<p class="note">共同样本（数值不同优先，黄色底行 = 份额/金额有差异）</p>' +
          '<div class="table-wrap"><table style="min-width:780px">' +
          '<thead><tr><th>标的</th><th>入场</th><th>出场</th><th class="num">基线份额</th><th class="num">对比份额</th><th class="num">基线入场额</th><th class="num">对比入场额</th><th class="num">基线出场额</th><th class="num">对比出场额</th><th class="num">基线收益</th><th class="num">对比收益</th></tr></thead>' +
          `<tbody>${candidate.samples.common.map(renderCommonTradeRow).join('')}</tbody>` +
          '</table></div>'
        : ''
      const addedTable = candidate.samples.added.length > 0
        ? '<p class="note">新增交易（相对基线）</p>' +
          '<div class="table-wrap"><table style="min-width:520px">' +
          '<thead><tr><th>标的</th><th>入场</th><th>出场</th><th class="num">份额</th><th class="num">入场金额</th><th class="num">收益</th></tr></thead>' +
          `<tbody>${candidate.samples.added.map(renderTradeSampleRow).join('')}</tbody>` +
          '</table></div>'
        : ''
      const removedTable = candidate.samples.removed.length > 0
        ? '<p class="note">消失交易（相对基线）</p>' +
          '<div class="table-wrap"><table style="min-width:520px">' +
          '<thead><tr><th>标的</th><th>入场</th><th>出场</th><th class="num">份额</th><th class="num">入场金额</th><th class="num">收益</th></tr></thead>' +
          `<tbody>${candidate.samples.removed.map(renderTradeSampleRow).join('')}</tbody>` +
          '</table></div>'
        : ''
      return `<div class="cand">` +
        `<div class="cand-head">${name} <span class="muted">${candidate.n_trades} 笔</span> · ${counts}</div>` +
        commonTable + addedTable + removedTable +
        '</div>'
    })
    .join('')
  return `${sectionHead}<p class="note">${baselineNote}</p>${blocks}</section>`
}

function renderCurvesSection(comparison: BacktestRunComparison): string {
  const summaryById = new Map(comparison.runs.map(run => [run.run_id, run]))
  const series: Array<{ name: string; color: string; points: Array<{ date: string; value: number }> }> = []
  const skipped: string[] = []
  comparison.curves?.forEach((curve, index) => {
    const summary = summaryById.get(curve.run_id)
    const name = summary ? runDisplayName(summary) : curve.run_id
    const points = curve.equity_curve ?? []
    // 空曲线不伪造：因子 run / 旧 run_card 没有账户净值，直接标记跳过
    const first = points.length > 0 ? Number(points[0].value ?? points[0].equity) : NaN
    if (points.length === 0 || !Number.isFinite(first) || first === 0) {
      skipped.push(name)
      return
    }
    const data: Array<{ date: string; value: number }> = []
    for (const point of points) {
      const value = Number(point.value ?? point.equity)
      if (!point.date || !Number.isFinite(value)) continue
      data.push({ date: String(point.date).slice(0, 10), value: value / first })
    }
    // 单点无法连成折线，同样按无曲线处理（与 SVG >=2 点过滤规则对齐）
    if (data.length < 2) {
      skipped.push(name)
      return
    }
    series.push({
      name,
      color: COMPARE_COLORS[index % COMPARE_COLORS.length],
      points: downsample(data, MAX_CURVE_POINTS),
    })
  })

  const skippedNote = skipped.length > 0
    ? `<p class="note">${escapeHtml(skipped.join('、'))} 无账户净值曲线（因子/旧记录），未参与绘图。</p>`
    : ''
  const sectionHead = [
    '<section id="curves">',
    '<h2>归一化净值曲线</h2>',
    '<p class="note">各 run 首日均归一为 1.0，仅比较相对走势，不代表真实资金。</p>',
  ]
  if (series.length === 0) {
    return [...sectionHead, '<p class="placeholder">所选 run 均无账户净值曲线，无法绘制对比图；因子 run 的分层/多空曲线请经详情 JSON 导出查看。</p>', skippedNote, '</section>'].join('\n')
  }
  const legend = `<p class="legend">${series
    .map(item => `<span class="legend-item"><span class="sw" style="background:${item.color}"></span>${escapeHtml(item.name)}</span>`)
    .join('')}</p>`
  const svg = buildLineChartSvg('归一化净值对比曲线', series, 280)
  return [...sectionHead, legend, svg, skippedNote, '</section>'].join('\n')
}

/**
 * 生成完整自包含 HTML 对比报告。
 * - 元信息（生成时间/Run 列表/标签）+ 指标矩阵（含 Δ）+ 配置差异 + 交易变动摘要 + 归一化净值 SVG
 * - 非有限指标显示「—」；缺 config_diff/trade_summary 的旧响应降级为占位说明
 */
export function buildCompareReportHtml(comparison: BacktestRunComparison, now: Date = new Date()): string {
  const generatedAt = escapeHtml(fmtDateTime(now.toISOString()))
  const parts: string[] = []
  parts.push('<!DOCTYPE html>')
  parts.push('<html lang="zh-CN">')
  parts.push('<head>')
  parts.push('<meta charset="utf-8">')
  parts.push('<meta name="viewport" content="width=device-width, initial-scale=1">')
  parts.push(`<title>${escapeHtml(`回测对比报告 · ${comparison.runs.length} 个运行`)}</title>`)
  parts.push(`<style>${COMPARE_REPORT_CSS}</style>`)
  parts.push('</head>')
  parts.push('<body>')
  parts.push('<header>')
  parts.push('<h1>回测对比报告</h1>')
  parts.push(`<p class="note">生成时间 ${generatedAt} · 共 ${comparison.runs.length} 个运行 · 首个运行为对比基线</p>`)
  parts.push('</header>')
  parts.push(renderMetaSection(comparison, now))
  parts.push(renderWarningsSection(comparison))
  parts.push(renderMatrixSection(comparison))
  parts.push(renderConfigDiffSection(comparison))
  parts.push(renderTradeSection(comparison))
  parts.push(renderCurvesSection(comparison))
  parts.push('<footer>')
  parts.push('<p>口径说明：指标矩阵首列为对比基线，Δ = 对比值 − 基线值；百分比指标的 Δ 为差值而非倍数，「—」表示该 run 无此指标或数值非有限。</p>')
  parts.push('<p>归一化净值曲线：各 run 首日归一为 1.0，仅比较相对走势，不代表真实资金；无账户净值的 run（因子/旧记录）未参与绘图。</p>')
  parts.push('<p>方法论提醒：跨区间/跨标的池/跨数据代的对比可能不可比，请结合「可比性提醒」与「配置差异」一并解读，指标口径以后端回测引擎输出为准。</p>')
  parts.push('<p>离线对比报告 · 自包含 HTML，可直接打印或存档</p>')
  parts.push('</footer>')
  parts.push('</body>')
  parts.push('</html>')
  return parts.join('\n')
}
