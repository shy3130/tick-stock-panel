/**
 * 市场环境(Regime)页 — 每日环境状态时序趋势 + 状态分布。
 *
 * 数据来源: 后端 regime_builder 批算的时序表(每日离散状态 + 多维指标)。
 * 不复刻 Dashboard 的当日总览(那是单日快照), 聚焦历史趋势与状态分布。
 *
 * 时间范围: 1年(250交易日) / 2年(500) / 自定义(1~1000天) / 全部(走日期范围)。
 * 美化对齐 Dashboard 设计语言: 半透明 surface 卡片 + 渐变竖条标题 + 语义色。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import {
  Activity, RefreshCw, Loader2, Gauge, TrendingUp, TrendingDown, Minus,
  Pencil, CalendarDays, Repeat, Rows3, LayoutGrid,
} from 'lucide-react'
import {
  api, type RegimeRow, type RegimeState,
  REGIME_STATE_LABELS, REGIME_STATE_COLORS,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'

const STATE_ORDER: RegimeState[] = ['strong', 'lean_strong', 'range', 'lean_weak', 'weak']

/** 综合分 → 对应状态色(与 classify_state 阈值一致: 70/55/45/30) */
function scoreToColor(score: number): string {
  if (score >= 70) return REGIME_STATE_COLORS.strong
  if (score >= 55) return REGIME_STATE_COLORS.lean_strong
  if (score >= 45) return REGIME_STATE_COLORS.range
  if (score >= 30) return REGIME_STATE_COLORS.lean_weak
  return REGIME_STATE_COLORS.weak
}

// ── 时间范围 ──────────────────────────────────────────────
// 1年=250 交易日, 2年=500 交易日; 自定义 1~1000; 全部走 start/end 日期范围。
type RangePreset = '1y' | '2y' | 'all' | { custom: number }

const RANGE_LABEL: Record<'1y' | '2y' | 'all', string> = {
  '1y': '1年', '2y': '2年', all: '全部',
}

/** 把 preset 解析成 (start?, end?, limit?) 三元组供 history 接口使用。 */
function resolveHistoryRange(
  preset: RangePreset,
  coverage: { earliest_date: string | null; latest_date: string | null } | undefined,
): { start?: string; end?: string; limit?: number } {
  if (preset === '1y') return { limit: 250 }
  if (preset === '2y') return { limit: 500 }
  if (preset === 'all') {
    // 全部: 用 coverage 实际日期范围, 不传 limit
    return { start: coverage?.earliest_date ?? undefined, end: coverage?.latest_date ?? undefined }
  }
  // 自定义天数
  return { limit: Math.max(1, Math.min(1000, preset.custom)) }
}

/** history/states 共用的"天数"语义: 用于 states 接口 + 标题展示。 */
function resolveDays(
  preset: RangePreset,
  coverage: { rows: number } | undefined,
): number {
  if (preset === '1y') return 250
  if (preset === '2y') return 500
  if (preset === 'all') return coverage?.rows && coverage.rows > 0 ? coverage.rows : 1000
  return Math.max(1, Math.min(1000, preset.custom))
}

function isPresetKey(p: RangePreset, k: '1y' | '2y' | 'all'): boolean {
  return p === k
}

// ── EChart hook ───────────────────────────────────────────
function useEChart(option: echarts.EChartsOption | null, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    if (!ref.current) return
    instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])
  useEffect(() => {
    if (instRef.current && option) instRef.current.setOption(option, { notMerge: true })
  }, [option, ...deps])
  return ref
}

// ── 页内通用 SectionTitle (对齐 Dashboard 渐变竖条风格) ────
function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-accent to-accent/30" />
      <Icon className="h-3.5 w-3.5 text-accent" />
      <h2 className="text-xs font-semibold text-foreground">{title}</h2>
      {hint != null && <span className="ml-auto text-[10px] text-muted font-mono">{hint}</span>}
    </div>
  )
}

// ── 卡片容器样式 (Dashboard 同款) ─────────────────────────
const cardCls = 'rounded-card border border-border bg-surface/80 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]'

// ── 主组件 ────────────────────────────────────────────────
export function Regime() {
  const qc = useQueryClient()
  const [range, setRange] = useState<RangePreset>('1y')
  const [customOpen, setCustomOpen] = useState(false)
  // 日历热力图显示模式: false=单行(月份网格横向排列+滚动条, 默认), true=展开(换行完整网格)
  const [calendarExpanded, setCalendarExpanded] = useState(false)
  const ct = useChartTheme()

  // coverage: "全部"模式 + 标题展示依赖
  const coverage = useQuery({
    queryKey: QK.regimeCoverage,
    queryFn: () => api.regimeCoverage(),
    staleTime: 5 * 60 * 1000,
  })

  const days = resolveDays(range, coverage.data)
  const histRange = resolveHistoryRange(range, coverage.data)

  // queryKey 用 range 的完整三元组区分: limit / start+end(全部) / custom天数
  const history = useQuery({
    queryKey: ['regime-history', range] as const,
    queryFn: () => api.regimeHistory(histRange.start, histRange.end, histRange.limit),
    staleTime: 5 * 60 * 1000,
  })
  const states = useQuery({
    queryKey: QK.regimeStates(days),
    queryFn: () => api.regimeStates(days),
    staleTime: 5 * 60 * 1000,
  })
  const [recomputing, setRecomputing] = useState(false)

  const rows: RegimeRow[] = history.data?.rows ?? []
  const latest = rows.length > 0 ? rows[rows.length - 1] : null

  // ── 当前势头: 末尾连续同态天数 + score 5日斜率(改善/恶化) + 上次弱势距今 ──
  const momentum = useMemo(() => {
    if (rows.length === 0) return null
    const lastState = rows[rows.length - 1].state
    // 末尾连续同态天数
    let streak = 1
    for (let i = rows.length - 2; i >= 0; i--) {
      if (rows[i].state === lastState) streak++
      else break
    }
    // score 5日斜率(正=改善, 负=恶化)
    const recent = rows.slice(-5)
    const slope = recent.length >= 2
      ? (recent[recent.length - 1].score - recent[0].score) / (recent.length - 1)
      : 0
    // 上次弱势(weak/lean_weak)距今天数
    let lastWeakGap = 0
    for (let i = rows.length - 1; i >= 0; i--) {
      if (rows[i].state === 'weak' || rows[i].state === 'lean_weak') {
        lastWeakGap = rows.length - 1 - i
        break
      }
    }
    return { streak, state: lastState, slope, lastWeakGap }
  }, [rows])

  // ── 状态转换频率: 近 N 天相邻 state 变化次数 ──
  const transitions = useMemo(() => {
    if (rows.length < 2) return { count: 0, rate: 0, label: '数据不足' }
    let count = 0
    for (let i = 1; i < rows.length; i++) {
      if (rows[i].state !== rows[i - 1].state) count++
    }
    // 频率 = 转换次数 / 天数; <0.2 稳定, 0.2-0.4 中等, >0.4 频繁
    const rate = count / (rows.length - 1)
    const label = rate < 0.2 ? '稳定' : rate < 0.4 ? '中等切换' : '频繁切换'
    return { count, rate, label }
  }, [rows])



  // 趋势图: 综合分主线 + 4 子维度曲线(可切换) + 状态背景色带 + 涨停数柱状
  const trendOption = useMemo<echarts.EChartsOption | null>(() => {
    if (rows.length === 0) return null
    const dates = rows.map(r => r.date)
    const scores = rows.map(r => r.score)
    const limitUps = rows.map(r => r.limit_up)
    const profit = rows.map(r => r.profit_score ?? null)
    const speculation = rows.map(r => r.speculation_score ?? null)
    const resilience = rows.map(r => r.resilience_score ?? null)
    const trend = rows.map(r => r.trend_score ?? null)

    // 状态背景色带: 合并连续同状态日期段, 每段用状态色低透明度着色
    const stateBands: any[] = []
    let bandStart = rows[0]?.date
    let prevState = rows[0]?.state
    rows.forEach((r, i) => {
      if (r.state !== prevState || i === rows.length - 1) {
        const bandEnd = i === rows.length - 1 ? r.date : rows[i - 1].date
        if (prevState && REGIME_STATE_COLORS[prevState as RegimeState]) {
          stateBands.push([
            { xAxis: bandStart, itemStyle: { color: REGIME_STATE_COLORS[prevState as RegimeState], opacity: 0.08 } },
            { xAxis: bandEnd },
          ])
        }
        bandStart = r.date
        prevState = r.state
      }
    })

    const subLineStyle = { width: 1.2, type: 'dotted' as const, opacity: 0.8 }

    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText } },
      legend: {
        data: ['综合分', '涨停数', '赚钱', '投机', '抗跌', '趋势'],
        textStyle: { color: ct.text, fontSize: 10 }, top: 0,
        // 默认只显示综合分 + 涨停数(简洁); 4 个子维度默认隐藏, 点图例展开看驱动因素
        selected: { '综合分': true, '涨停数': true, '赚钱': false, '投机': false, '抗跌': false, '趋势': false },
      },
      grid: { left: 48, right: 64, top: 36, bottom: 56 },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: string) => v.slice(5) },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '涨停', position: 'left', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
        { type: 'value', name: '综合分', min: 0, max: 100, position: 'right', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
      ],
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - (60 / days) * 100) },
        { type: 'slider', bottom: 8, height: 16, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        // 涨停数柱状(半透明背景, 左轴)
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 0, barMaxWidth: 6,
          itemStyle: { color: REGIME_STATE_COLORS.strong, opacity: 0.35 }, z: 1 },
        // 4 子维度曲线(右轴=综合分): 帮助理解综合分由什么驱动(点图例可切换)
        { name: '赚钱', type: 'line', data: profit, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#f59e0b' }, z: 2 },
        { name: '投机', type: 'line', data: speculation, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#a855f7' }, z: 2 },
        { name: '抗跌', type: 'line', data: resilience, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#10b981' }, z: 2 },
        { name: '趋势', type: 'line', data: trend, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { ...subLineStyle, color: '#3b82f6' }, z: 2 },
        // 综合分主线(加粗置顶, 右轴) + 状态背景色带 + 阈值横虚线
        { name: '综合分', type: 'line', data: scores, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.5, color: ct.textStrong }, areaStyle: { opacity: 0.06 }, z: 3,
          markArea: { silent: true, data: stateBands },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { type: 'dashed', width: 1.5 },
            label: { position: 'end', fontSize: 10, fontWeight: 'bold', padding: [2, 4], borderRadius: 3 },
            data: [
              { yAxis: 70, lineStyle: { color: REGIME_STATE_COLORS.strong },
                label: { formatter: '强势 70', color: '#fff', backgroundColor: REGIME_STATE_COLORS.strong } },
              { yAxis: 55, lineStyle: { color: REGIME_STATE_COLORS.lean_strong },
                label: { formatter: '偏强 55', color: '#fff', backgroundColor: REGIME_STATE_COLORS.lean_strong } },
              { yAxis: 45, lineStyle: { color: REGIME_STATE_COLORS.range },
                label: { formatter: '震荡 45', color: '#fff', backgroundColor: REGIME_STATE_COLORS.range } },
              { yAxis: 30, lineStyle: { color: REGIME_STATE_COLORS.lean_weak },
                label: { formatter: '偏弱 30', color: '#fff', backgroundColor: REGIME_STATE_COLORS.lean_weak } },
            ],
          } },
      ],
    }
  }, [rows, days, ct])
  const trendRef = useEChart(trendOption, [trendOption])

  // 状态分布饼图
  const pieOption = useMemo<echarts.EChartsOption | null>(() => {
    const dist = states.data?.distribution ?? []
    if (dist.length === 0) return null
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText } },
      series: [{
        type: 'pie', radius: ['42%', '65%'], center: ['50%', '52%'],
        // 标签外置 + 引导虚线, 避免 5 个状态标签互相重叠(原 label 紧贴扇区会挤在一起)。
        label: {
          position: 'outside',
          color: ct.text, fontSize: 10,
          formatter: '{b}  {d}%',
        },
        labelLine: {
          show: true,
          length: 8,        // 第一段(扇区到拐点)
          length2: 10,      // 第二段(拐点到标签)
          lineStyle: { color: ct.border, type: 'dashed', width: 1 },
        },
        // labelLayout 自动调整标签位置防重叠: 相邻标签过近时自动错开
        labelLayout: { hideOverlap: false },
        data: STATE_ORDER
          .map(s => dist.find(d => d.state === s))
          .filter((x): x is NonNullable<typeof x> => !!x)
          .map(d => ({
            name: d.label, value: d.count,
            itemStyle: { color: REGIME_STATE_COLORS[d.state] },
          })),
      }],
    }
  }, [states.data, ct])
  const pieRef = useEChart(pieOption, [pieOption])

  // 日历热力图数据: 按月分组(纯 CSS 渲染, 不依赖 echarts calendar 的跨年怪异行为)。
  // 结构: [{ year, month, label, weeks: [[cell|gap]×7]×N }]
  // 每个 cell = { date, score, state } 或 null(该位置无交易日, 如月初前的空位)
  const calendarMonths = useMemo(() => {
    if (rows.length === 0) return []
    const byMonth = new Map<string, RegimeRow[]>()
    for (const r of rows) {
      const ym = r.date.slice(0, 7) // YYYY-MM
      if (!byMonth.has(ym)) byMonth.set(ym, [])
      byMonth.get(ym)!.push(r)
    }
    const MONTH_LABELS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']
    return [...byMonth.entries()].sort().map(([ym, monthRows]) => {
      const [y, m] = ym.split('-')
      const year = Number(y), month = Number(m)
      // 按自然日铺格子: 只有该日为交易日且有数据才填 RegimeRow, 其余日子填 null。
      // 这样每个格子都对其正确的周几列, 不会因月内数据不连续而把交易日错位到周末列。
      const dateToRow = new Map<string, RegimeRow>()
      for (const r of monthRows) dateToRow.set(r.date, r)
      // 月首的星期偏移(周一=0..周日=6), 与表头 ['一'..'日'] 列顺序一致
      const firstDow = new Date(year, month - 1, 1).getDay()
      const leadOffset = firstDow === 0 ? 6 : firstDow - 1
      const cells: (RegimeRow | null)[] = Array(leadOffset).fill(null)
      const dayCount = new Date(year, month, 0).getDate()
      for (let day = 1; day <= dayCount; day++) {
        const ds = `${y}-${m.padStart(2, '0')}-${String(day).padStart(2, '0')}`
        cells.push(dateToRow.has(ds) ? dateToRow.get(ds)! : null)
      }
      // 补齐到 7 的倍数(完整周)
      while (cells.length % 7 !== 0) cells.push(null)
      // 切成每周一组
      const weeks: (RegimeRow | null)[][] = []
      for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
      return { year, month, label: `${y}年${MONTH_LABELS[month - 1]}`, weeks }
    })
  }, [rows])

  // 日历热力图横向滚动容器: 单行模式下默认滚到最右侧(显示最新月份)
  const calendarScrollRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!calendarExpanded && calendarScrollRef.current) {
      calendarScrollRef.current.scrollLeft = calendarScrollRef.current.scrollWidth
    }
  }, [calendarExpanded, calendarMonths])

  const handleRecompute = async () => {
    setRecomputing(true)
    try {
      const r = await api.regimeRecompute()
      toast(r.computed > 0 ? `重算完成 · 新增 ${r.computed} 天` : '重算完成 · 数据已是最新', 'success')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['regime-history'] }),
        qc.invalidateQueries({ queryKey: ['regime-states'] }),
        qc.invalidateQueries({ queryKey: ['regime-latest'] }),
        qc.invalidateQueries({ queryKey: QK.regimeCoverage }),
      ])
    } catch (e) {
      toast(`重算失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setRecomputing(false)
    }
  }

  // 自定义按钮标签
  const customLabel = typeof range === 'object'
    ? `自定义 ${range.custom}天`
    : '自定义'

  return (
    <div className="mx-auto max-w-[1440px] px-4 py-5 space-y-4">
      {/* ── 头部 (Dashboard 渐变条卡片) ── */}
      <div className={cn(cardCls, 'relative overflow-hidden rounded-card bg-gradient-to-r from-surface/90 to-surface/70 px-4 py-3')}>
        <div className="absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" />
        <div className="flex items-center gap-3">
          <Activity className="h-5 w-5 text-accent" />
          <h1 className="text-base font-semibold text-foreground">市场环境</h1>
          <span className="text-xs text-muted">每日环境状态 · 赚钱效应 · 趋势分析</span>
          <div className="ml-auto flex items-center gap-2">
            {/* 时间范围按钮组 */}
            <div className="flex items-center rounded-btn border border-border bg-base/60 p-0.5">
              {(['1y', '2y', 'all'] as const).map(k => (
                <button
                  key={k}
                  onClick={() => setRange(k)}
                  className={cn(
                    'h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                    isPresetKey(range, k)
                      ? 'bg-accent text-white shadow-sm'
                      : 'text-secondary hover:text-foreground',
                  )}
                >
                  {RANGE_LABEL[k]}
                </button>
              ))}
              <button
                onClick={() => setCustomOpen(true)}
                className={cn(
                  'inline-flex items-center gap-1 h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                  typeof range === 'object'
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-secondary hover:text-foreground',
                )}
              >
                {typeof range === 'object' && <Pencil className="h-3 w-3" />}
                {customLabel}
              </button>
            </div>
            {/* 重算 */}
            <button onClick={handleRecompute} disabled={recomputing}
              className="inline-flex items-center gap-1.5 h-7 px-3 rounded-btn border border-border bg-base text-xs text-secondary hover:text-accent disabled:opacity-50">
              {recomputing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {recomputing ? '重算中…' : '重算'}
            </button>
          </div>
        </div>
      </div>

      {/* ── 最新日概览 (4 个指标卡, 去掉与看板重复的涨停/涨跌/成交额) ── */}
      {latest ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {/* 状态卡(保留) */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Gauge className="h-3 w-3" /> 最新状态 · {latest.date}
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="text-2xl font-bold" style={{ color: REGIME_STATE_COLORS[latest.state] }}>
                {REGIME_STATE_LABELS[latest.state]}
              </span>
              <span className="text-sm text-muted">{latest.score} 分</span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${Math.max(2, Math.min(100, latest.score))}%`, backgroundColor: REGIME_STATE_COLORS[latest.state] }} />
            </div>
          </div>

          {/* 当前势头(新) */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              {(() => {
                const TrendIcon = (momentum?.slope ?? 0) > 0.5 ? TrendingUp : (momentum?.slope ?? 0) < -0.5 ? TrendingDown : Minus
                return <TrendIcon className={`h-3 w-3 ${(momentum?.slope ?? 0) > 0.5 ? 'text-bull' : (momentum?.slope ?? 0) < -0.5 ? 'text-bear' : 'text-muted'}`} />
              })()} 当前势头
            </div>
            {momentum ? (
              <>
                <div className="mt-1.5 text-sm font-semibold text-foreground">
                  连续 <span style={{ color: REGIME_STATE_COLORS[momentum.state] }}>{momentum.streak}</span> 天{REGIME_STATE_LABELS[momentum.state]}
                </div>
                <div className="mt-1 text-[10px] text-muted">
                  5日{(momentum.slope > 0 ? '改善' : momentum.slope < 0 ? '恶化' : '持平')}
                  {momentum.lastWeakGap > 0 && ` · 上次弱势 ${momentum.lastWeakGap} 天前`}
                </div>
              </>
            ) : <div className="mt-1.5 text-sm text-muted">—</div>}
          </div>

          {/* 4 子维度迷你条(新) */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Activity className="h-3 w-3" /> 四维拆解 · {latest.date}
            </div>
            <div className="mt-2 space-y-1">
              {([
                { label: '赚钱', val: latest.profit_score, color: '#f59e0b' },
                { label: '投机', val: latest.speculation_score, color: '#a855f7' },
                { label: '抗跌', val: latest.resilience_score, color: '#10b981' },
                { label: '趋势', val: latest.trend_score, color: '#3b82f6' },
              ] as const).map(d => (
                <div key={d.label} className="flex items-center gap-1.5">
                  <span className="w-6 shrink-0 text-[9px] text-muted">{d.label}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-base">
                    <div className="h-full rounded-full transition-all"
                      style={{ width: `${d.val ?? 0}%`, backgroundColor: d.color }} />
                  </div>
                  <span className="w-5 shrink-0 text-right text-[9px] font-mono text-muted">{d.val ?? '—'}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 状态转换频率(新) */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Repeat className="h-3 w-3" /> 状态转换 · 近 {days} 天
            </div>
            <div className="mt-1.5 text-lg font-semibold text-foreground">
              {transitions.count} <span className="text-xs font-normal text-muted">次切换</span>
            </div>
            <div className="mt-1 text-[10px] text-muted">
              节奏：<span className="text-accent">{transitions.label}</span>
              <span className="ml-1">({(transitions.rate * 100).toFixed(0)}%/天)</span>
            </div>
          </div>
        </div>
      ) : (
        <div className="rounded-card border border-dashed border-border p-8 text-center text-sm text-muted">
          {history.isLoading ? '加载中…' : '暂无环境数据，请先运行盘后管道或点击「重算」'}
        </div>
      )}

      {/* ── 状态色带时间轴 ── */}
      {rows.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Activity} title="状态时间轴"
            hint={`${rows[0]?.date} → ${rows[rows.length - 1]?.date} · ${rows.length} 天`} />
          <div className="mt-2.5 flex h-7 w-full overflow-hidden rounded-md">
            {rows.map(r => (
              <div key={r.date} title={`${r.date} ${REGIME_STATE_LABELS[r.state]}(${r.score})`}
                className="flex-1 min-w-[2px] transition-opacity hover:opacity-80"
                style={{ backgroundColor: REGIME_STATE_COLORS[r.state] }} />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted">
            {STATE_ORDER.map(s => (
              <span key={s} className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded"
                  style={{ backgroundColor: REGIME_STATE_COLORS[s] }} />
                {REGIME_STATE_LABELS[s]}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ── 趋势图 + 分布图 ── */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className={cn(cardCls, 'p-3 lg:col-span-2')}>
          <SectionTitle icon={Activity} title="环境综合分趋势"
            hint="综合分(粗) · 赚钱/投机/抗跌/趋势(细, 可点图例切换) · 背景色=状态" />
          <div ref={trendRef} className="mt-2 h-[320px]" />
        </div>
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Gauge} title="状态分布" hint={`近 ${days} 天`} />
          <div ref={pieRef} className="mt-2 h-[320px]" />
        </div>
      </div>

      {/* ── 日历热力图(每日综合分按状态色, 支持单行/展开切换) ── */}
      {calendarMonths.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={CalendarDays} title="日历热力图"
            hint={
              <button
                onClick={() => setCalendarExpanded(v => !v)}
                className="inline-flex items-center gap-1 rounded-btn border border-border bg-base px-2 py-0.5 text-[10px] text-secondary hover:text-accent hover:border-accent/40 transition-colors"
                title={calendarExpanded ? '切换为单行紧凑' : '切换为月份展开'}
              >
                {calendarExpanded ? <><Rows3 className="h-3 w-3" />单行</> : <><LayoutGrid className="h-3 w-3" />展开</>}
              </button>
            }
          />
          {calendarExpanded ? (
            /* 展开模式: 按月分块的完整日历网格, 自动换行 */
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-3">
              {calendarMonths.map(mo => {
                const monthRows = mo.weeks.flat().filter((c): c is RegimeRow => !!c)
                const avgScore = monthRows.length > 0
                  ? Math.round(monthRows.reduce((s, r) => s + r.score, 0) / monthRows.length) : 0
                return (
                  <div key={`${mo.year}-${mo.month}`} className="shrink-0">
                    <div className="mb-1 flex items-center gap-1.5">
                      <span className="text-[10px] font-medium text-secondary">{mo.label}</span>
                      {avgScore > 0 && (
                        <span className="rounded px-1 py-px text-[9px] font-semibold"
                          style={{ color: scoreToColor(avgScore), backgroundColor: scoreToColor(avgScore) + '20' }}>
                          {avgScore}
                        </span>
                      )}
                    </div>
                    <div className="mb-0.5 grid grid-cols-7 gap-[2px] text-[8px] text-muted">
                      {['一', '二', '三', '四', '五', '六', '日'].map(d => (
                        <div key={d} className="text-center">{d}</div>
                      ))}
                    </div>
                    <div className="grid grid-cols-7 gap-[2px]">
                      {mo.weeks.flat().map((cell, i) => (
                        cell ? (
                          <div key={i}
                            title={`${cell.date} ${REGIME_STATE_LABELS[cell.state]}(${cell.score})`}
                            className="h-[14px] w-[14px] rounded-[2px] transition-transform hover:scale-125 hover:z-10 cursor-default"
                            style={{ backgroundColor: REGIME_STATE_COLORS[cell.state] }}
                          />
                        ) : (
                          <div key={i} className="h-[14px] w-[14px]" />
                        )
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          ) : (
            /* 单行模式(默认): 月份网格横向一行+滚动条, 默认滚到最新, 每月份带月均分 */
            <div ref={calendarScrollRef} className="mt-3 flex gap-x-5 overflow-x-auto pb-2">
              {calendarMonths.map(mo => {
                const monthRows = mo.weeks.flat().filter((c): c is RegimeRow => !!c)
                const avgScore = monthRows.length > 0
                  ? Math.round(monthRows.reduce((s, r) => s + r.score, 0) / monthRows.length) : 0
                return (
                  <div key={`${mo.year}-${mo.month}`} className="shrink-0">
                    <div className="mb-1 flex items-center gap-1.5">
                      <span className="text-[10px] font-medium text-secondary">{mo.label}</span>
                      {avgScore > 0 && (
                        <span className="rounded px-1 py-px text-[9px] font-semibold"
                          style={{ color: scoreToColor(avgScore), backgroundColor: scoreToColor(avgScore) + '20' }}>
                          {avgScore}
                        </span>
                      )}
                    </div>
                    <div className="mb-0.5 grid grid-cols-7 gap-[2px] text-[8px] text-muted">
                      {['一', '二', '三', '四', '五', '六', '日'].map(d => (
                        <div key={d} className="text-center">{d}</div>
                      ))}
                    </div>
                    <div className="grid grid-cols-7 gap-[2px]">
                      {mo.weeks.flat().map((cell, i) => (
                        cell ? (
                          <div key={i}
                            title={`${cell.date} ${REGIME_STATE_LABELS[cell.state]}(${cell.score})`}
                            className="h-[14px] w-[14px] rounded-[2px] transition-transform hover:scale-125 hover:z-10 cursor-default"
                            style={{ backgroundColor: REGIME_STATE_COLORS[cell.state] }}
                          />
                        ) : (
                          <div key={i} className="h-[14px] w-[14px]" />
                        )
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* ── 自定义天数弹窗 ── */}
      {customOpen && (
        <CustomDaysModal
          current={typeof range === 'object' ? range.custom : 120}
          onClose={() => setCustomOpen(false)}
          onApply={(d) => { setRange({ custom: d }); setCustomOpen(false) }}
        />
      )}
    </div>
  )
}

// ── 自定义天数输入弹窗 ────────────────────────────────────
function CustomDaysModal({ current, onClose, onApply }: {
  current: number
  onClose: () => void
  onApply: (days: number) => void
}) {
  const [val, setVal] = useState(String(current))
  const inputRef = useRef<HTMLInputElement>(null)

  const apply = () => {
    const n = Math.max(1, Math.min(1000, Math.floor(Number(val) || 0)))
    if (Number.isNaN(n) || n < 1) {
      toast('请输入 1 ~ 1000 之间的天数', 'error')
      return
    }
    onApply(n)
  }

  return (
    <Modal onClose={onClose} ariaLabel="自定义天数" initialFocusRef={inputRef}
      panelClassName="w-[88vw] max-w-xs bg-surface border border-border rounded-card shadow-xl p-4">
      <div className="space-y-3">
        <div>
          <div className="text-xs font-medium text-foreground">自定义天数</div>
          <div className="mt-0.5 text-[10px] text-muted">范围 1 ~ 1000 个交易日</div>
        </div>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={1000}
          value={val}
          onChange={e => setVal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') apply() }}
          className="h-8 w-full rounded-input border border-border bg-base px-2.5 text-sm text-foreground outline-none focus:border-accent"
        />
        {/* 快捷预设 */}
        <div className="flex flex-wrap gap-1.5">
          {[60, 90, 180, 365].map(d => (
            <button key={d} onClick={() => setVal(String(d))}
              className="h-6 rounded-btn border border-border bg-base px-2 text-[11px] text-secondary hover:text-accent hover:border-accent/40 transition-colors">
              {d}天
            </button>
          ))}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose}
            className="h-7 rounded-btn px-3 text-xs text-secondary hover:text-foreground transition-colors">
            取消
          </button>
          <button onClick={apply}
            className="h-7 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 transition-colors">
            应用
          </button>
        </div>
      </div>
    </Modal>
  )
}
