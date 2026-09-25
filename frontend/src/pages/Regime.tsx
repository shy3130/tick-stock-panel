/**
 * 市场环境(Regime)页 — 每日环境状态时序趋势 + 状态分布。
 *
 * 数据来源: 后端 regime_builder 批算的时序表(每日离散状态 + 多维指标)。
 * 不复刻 Dashboard 的当日总览(那是单日快照), 聚焦历史趋势与状态分布。
 *
 * 时间范围: 1年(250交易日) / 2年(500) / 自定义(1~1000天) / 全部(走日期范围)。
 * 美化对齐 Dashboard 设计语言: 半透明 surface 卡片 + 渐变竖条标题 + 语义色。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import * as echarts from 'echarts'
import {
  Activity, RefreshCw, Loader2, Gauge, TrendingUp, TrendingDown, Minus,
  Pencil, CalendarDays, Repeat, Rows3, LayoutGrid, Flame, Layers, Filter, X,
} from 'lucide-react'
import {
  api, type RegimeRow, type RegimeState, type MarketPhase,
  REGIME_STATE_LABELS, REGIME_STATE_COLORS,
  MARKET_PHASE_LABELS, MARKET_PHASE_COLORS, MARKET_PHASE_ORDER,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'

const STATE_ORDER: RegimeState[] = ['strong', 'lean_strong', 'range', 'lean_weak', 'weak']

/** 阶段含义与应对提示 — meaning 与后端 market_phase.py 判定规则对齐, 供当前阶段卡展示 */
const MARKET_PHASE_GUIDE: Record<MarketPhase, { meaning: string; action: string }> = {
  ice:     { meaning: '高度/宽度/首板同时贴地, 亏钱效应极致', action: '观望 · 跟踪率先异动股' },
  ignite:  { meaning: '低位放量扩张, 晋级率回升', action: '试仓主线 · 快进快出' },
  rally:   { meaning: '高度+宽度+晋级率共振', action: '持股 · 顺势而为' },
  climax:  { meaning: '情绪极端宣泄, 批量二板+', action: '逐步兑现 · 不追高' },
  ebb:     { meaning: '自高位回落, 晋级率坍塌', action: '防守 · 不接力' },
  repair:  { meaning: '多空拉锯, 无明确方向', action: '轻仓试错 · 控回撤' },
}

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
function useEChart(
  option: echarts.EChartsOption | null,
  deps: unknown[],
  onReady?: (inst: echarts.ECharts) => void,
) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])
  useEffect(() => {
    if (!ref.current) return
    // 惰性 init: 图表容器可能条件渲染晚于组件挂载 (如情绪周期图依赖异步查询结果,
    // 冷加载时首帧 rows 为空 → div 不在 DOM, 仅挂载时跑一次的 init 会扑空)。
    // 数据到达后 option 变化触发本 effect, 此时 div 已挂载 — 补建实例再 setOption。
    if (!instRef.current) {
      instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
      onReady?.(instRef.current)
    }
    if (option) {
      instRef.current.setOption(option, { notMerge: true })
      // 容器可能经历 display:none(tab 隐藏) → 可见的切换, 画布尺寸需要按当前
      // 容器实际尺寸重算; 调用方把 view 等显隐依赖传入 deps 以触发本 effect。
      instRef.current.resize()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  // 视图 tab: 市场环境(状态/趋势/日历) 与 情绪周期(阶段/主线) 两组内容同页切换,
  // 避免单页过长需要大幅滚动。两组共用时间范围与重算入口。
  const [view, setView] = useState<'regime' | 'phase'>('regime')
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
  // 情绪周期阶段段 + 主线排行(与 history 同一时间范围)
  const phases = useQuery({
    queryKey: QK.regimePhases(histRange.start, histRange.end),
    queryFn: () => api.regimePhases(histRange.start, histRange.end),
    staleTime: 5 * 60 * 1000,
  })
  const [mainlineKind, setMainlineKind] = useState<'concept' | 'industry'>('concept')
  const [filterOpen, setFilterOpen] = useState(false)
  // 时间轴点击选中的交易日 (当日快照联动); null = 未选。窗口切换后失效。
  const [selDate, setSelDate] = useState<string | null>(null)
  useEffect(() => { setSelDate(null) }, [histRange.start, histRange.end])
  const mainline = useQuery({
    queryKey: QK.regimeMainline(mainlineKind, histRange.start, histRange.end),
    queryFn: () => api.regimeMainline(histRange.start, histRange.end, 10, mainlineKind),
    staleTime: 5 * 60 * 1000,
  })
  const [recomputing, setRecomputing] = useState(false)

  const rows: RegimeRow[] = history.data?.rows ?? []
  const latest = rows.length > 0 ? rows[rows.length - 1] : null
  const hasPhaseData = rows.length > 0 && rows.some(r => r.phase != null)
  const segments = phases.data?.segments ?? []

  // 当前阶段持续天数(末尾连续同阶段) + 当前主线(最新交易日 top3)
  const phaseStreak = useMemo(() => {
    if (!hasPhaseData) return null
    const lastPhase = rows[rows.length - 1].phase
    let streak = 1
    for (let i = rows.length - 2; i >= 0; i--) {
      if (rows[i].phase === lastPhase) streak++
      else break
    }
    return { phase: lastPhase as MarketPhase, streak }
  }, [rows, hasPhaseData])
  const latestMainlines = useMemo(() => {
    const mlRows = mainline.data?.rows ?? []
    if (mlRows.length === 0) return []
    const lastDate = mlRows[mlRows.length - 1].date
    return mlRows.filter(r => r.date === lastDate && r.rank <= 3)
  }, [mainline.data])

  // ── 时间轴点击选中日: 当日行 + 当日主线 top3 (主查询窗口内可回看任意一天) ──
  const selRow = useMemo(
    () => (selDate ? rows.find(r => r.date === selDate) ?? null : null),
    [rows, selDate],
  )
  const selMainlines = useMemo(() => {
    if (!selDate) return []
    return (mainline.data?.rows ?? []).filter(r => r.date === selDate && r.rank <= 3)
  }, [mainline.data, selDate])
  // 选中日处于其阶段段的第几天 (自段首数起, 与"当前阶段第 N 天"同口径)
  const selStreak = useMemo(() => {
    if (!selRow?.phase) return null
    let streak = 1
    for (let i = rows.findIndex(r => r.date === selRow.date); i > 0; i--) {
      if (rows[i - 1].phase === selRow.phase) streak++
      else break
    }
    return streak
  }, [rows, selRow])

  // ── 指标历史分位: 最新值在当前窗口内的位置 (≤ 它的天数占比), 给指标卡参照系 ──
  const pctRank = useCallback((field: keyof RegimeRow): number | null => {
    if (!latest) return null
    const latestV = latest[field] as number | null
    if (latestV == null) return null
    const vals = rows.map(r => r[field] as number | null).filter((v): v is number => v != null)
    if (vals.length < 20) return null   // 样本太少分位无意义
    const below = vals.filter(v => v <= latestV).length
    return Math.round((100 * below) / vals.length)
  }, [rows, latest])

  // ── 阶段规律: 当前阶段的历史段统计 + 下一阶段转移分布 (窗口内 segments) ──
  const phaseStats = useMemo(() => {
    if (!phaseStreak || segments.length === 0) return null
    const cur = phaseStreak.phase
    const segs = segments.filter(s => s.phase === cur)
    if (segs.length === 0) return null
    const avgDays = Math.round(segs.reduce((a, s) => a + s.days, 0) / segs.length)
    const maxDays = Math.max(...segs.map(s => s.days))
    // 转移分布: 每个历史同阶段段的后继段 (segments 按开始日期升序, 最后一段无后继不计)
    const nexts = new Map<MarketPhase, number>()
    segments.forEach((s, i) => {
      if (s.phase !== cur) return
      const nx = segments[i + 1]
      if (!nx) return
      nexts.set(nx.phase as MarketPhase, (nexts.get(nx.phase as MarketPhase) ?? 0) + 1)
    })
    const total = [...nexts.values()].reduce((a, b) => a + b, 0)
    const transitions = [...nexts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([phase, count]) => ({ phase, count, pct: total > 0 ? Math.round((100 * count) / total) : 0 }))
    return { count: segs.length, avgDays, maxDays, transitions }
  }, [phaseStreak, segments])

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



  // 阶段时间轴: 高度折线 + 2板以上宽度柱 + 晋级率曲线, 背景色带=情绪周期阶段
  const phaseOption = useMemo<echarts.EChartsOption | null>(() => {
    if (rows.length === 0 || !hasPhaseData) return null
    const dates = rows.map(r => r.date)
    const heights = rows.map(r => r.max_consecutive)
    const firstBoard = rows.map(r => r.first_board ?? null)
    const ge2 = rows.map(r => r.ge2_count ?? null)
    const promo = rows.map(r => (r.promo_rate != null ? Math.round(r.promo_rate * 100) : null))
    const seal = rows.map(r => (r.seal_rate != null ? Math.round(r.seal_rate * 100) : null))
    // 阶段色带: 连续同阶段为一带; ≥6 天的宽带标阶段名(窄带不标避免糊作一团)
    const phaseBands: any[] = []
    let bandStartIdx = 0
    let prevPhase = rows[0]?.phase
    rows.forEach((r, i) => {
      if (r.phase !== prevPhase || i === rows.length - 1) {
        const endIdx = i === rows.length - 1 ? i : i - 1
        if (prevPhase && MARKET_PHASE_COLORS[prevPhase as MarketPhase]) {
          const color = MARKET_PHASE_COLORS[prevPhase as MarketPhase]
          const bandDays = endIdx - bandStartIdx + 1
          phaseBands.push([
            {
              xAxis: rows[bandStartIdx].date,
              itemStyle: { color, opacity: 0.10 },
              label: {
                show: bandDays >= 6,
                formatter: MARKET_PHASE_LABELS[prevPhase as MarketPhase],
                position: 'insideTopLeft', distance: 6,
                color, fontSize: 9, fontWeight: 600,
              },
            },
            { xAxis: rows[endIdx].date },
          ])
        }
        bandStartIdx = i
        prevPhase = r.phase
      }
    })
    // 阶段切换点: 当日 phase ≠ 前日 → 在高度线上标一枚新阶段颜色的小三角
    const switchPoints: any[] = []
    for (let i = 1; i < rows.length; i++) {
      if (rows[i].phase && rows[i].phase !== rows[i - 1].phase) {
        switchPoints.push({
          coord: [rows[i].date, heights[i]],
          itemStyle: { color: MARKET_PHASE_COLORS[rows[i].phase as MarketPhase] },
          label: { show: false },
        })
      }
    }
    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis', backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText },
        axisPointer: { type: 'line', snap: true, lineStyle: { color: ct.grid } },
        formatter: (params: any) => {
          const p0 = Array.isArray(params) ? params[0] : params
          const i = dates.indexOf(p0.axisValue)
          const r = rows[i]
          if (!r) return ''
          const phase = r.phase ? MARKET_PHASE_LABELS[r.phase] : '—'
          // 该日处于阶段段的第几天 (与"当前阶段第 N 天"同口径)
          let dayN = 1
          for (let j = i; j > 0; j--) {
            if (rows[j - 1].phase === r.phase) dayN++
            else break
          }
          const pct = (v: number | null | undefined) =>
            v != null ? (v * 100).toFixed(1) + '%' : '—'
          return [
            `<b>${r.date}</b> · ${phase} 第${dayN}天`,
            `高度 ${r.max_consecutive}板 · 完整度 ${r.ladder_completeness != null ? (r.ladder_completeness * 100).toFixed(0) + '%' : '—'}`,
            `涨停 ${r.limit_up ?? '—'}家 = 首板 ${r.first_board ?? '—'} + 2板+ ${r.ge2_count ?? '—'}`,
            `晋级率 ${pct(r.promo_rate)} · 封板率 ${pct(r.seal_rate)}`,
          ].join('<br/>')
        },
      },
      legend: {
        data: ['首板', '2板+', '高度', '晋级率', '封板率'],
        selected: { 封板率: false },
        textStyle: { color: ct.text, fontSize: 10 }, top: 0,
      },
      grid: { left: 44, right: 44, top: 32, bottom: 44 },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: string) => v.slice(5) },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '板/家', position: 'left', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: ct.text } },
        { type: 'value', name: '比率%', min: 0, max: 100, position: 'right', axisLabel: { color: ct.text, fontSize: 10 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
      ],
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - (60 / days) * 100) },
        { type: 'slider', bottom: 6, height: 14, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        // 首板/2板+ 堆叠柱: 两段之和 = 当日涨停总数 (首板宽度=底部, 2板+=顶段),
        // 总高看涨停宽度、顶段看连板厚度; 高度线在其上穿行
        { name: '首板', type: 'bar', stack: 'lu', data: firstBoard, yAxisIndex: 0,
          barMaxWidth: 5, itemStyle: { color: '#f97316', opacity: 0.35 }, z: 1 },
        { name: '2板+', type: 'bar', stack: 'lu', data: ge2, yAxisIndex: 0,
          barMaxWidth: 5, itemStyle: { color: '#f59e0b', opacity: 0.65 }, z: 1 },
        { name: '高度', type: 'line', data: heights, smooth: true, symbol: 'none', yAxisIndex: 0,
          lineStyle: { width: 1.6, color: '#ef4444' }, z: 3,
          markArea: { silent: true, data: phaseBands },
          // 定位竖线: 今日(淡白) + 选中日(蓝虚线, 点击图表日期出现)
          markLine: {
            silent: true, symbol: 'none',
            data: [
              {
                xAxis: dates[dates.length - 1],
                lineStyle: { color: ct.text, opacity: 0.35, width: 1 },
                label: { show: true, position: 'end', formatter: '今日', color: ct.text, fontSize: 9 },
              },
              ...(selDate && selDate !== dates[dates.length - 1]
                ? [{
                    xAxis: selDate,
                    lineStyle: { color: '#3b82f6', type: 'dashed' as const, width: 1.2, opacity: 0.9 },
                    label: { show: true, position: 'end' as const, formatter: selDate.slice(5), color: '#3b82f6', fontSize: 9 },
                  } as const]
                : []),
            ],
          },
          // 阶段切换点: 小三角=当日切换, 颜色=新阶段
          markPoint: {
            silent: true, symbol: 'triangle', symbolSize: 7,
            data: switchPoints,
          } },
        { name: '晋级率', type: 'line', data: promo, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.2, color: '#3b82f6', type: 'dotted' }, z: 2 },
        { name: '封板率', type: 'line', data: seal, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.2, color: '#10b981', type: 'dashed' }, z: 2 },
      ],
    }
  }, [rows, days, ct, hasPhaseData, selDate])
  const [phaseChartInst, setPhaseChartInst] = useState<echarts.ECharts | null>(null)
  const phaseChartRef = useEChart(phaseOption, [phaseOption, view], setPhaseChartInst)
  // 点击图表任意位置 → 选中最近的交易日 (zrender 级监听, 命中区为整个网格,
  // 不依赖细线/窄柱的精确点击); 点击图例/dataZoom 不在网格内, 自动忽略
  useEffect(() => {
    if (!phaseChartInst || !phaseOption) return
    const zr = phaseChartInst.getZr()
    const onClick = (e: { offsetX: number; offsetY: number }) => {
      if (!phaseChartInst.containPixel('grid', [e.offsetX, e.offsetY])) return
      const dates = rows.map(r => r.date)
      const idx = Math.round(phaseChartInst.convertFromPixel({ seriesIndex: 0 }, [e.offsetX, e.offsetY])[0])
      if (Number.isFinite(idx) && idx >= 0 && idx < dates.length) setSelDate(dates[idx])
    }
    zr.on('click', onClick)
    return () => { zr.off('click', onClick) }
  }, [phaseChartInst, phaseOption, rows])

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
  const trendRef = useEChart(trendOption, [trendOption, view])

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
  const pieRef = useEChart(pieOption, [pieOption, view])

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
        qc.invalidateQueries({ queryKey: ['regime-phases'] }),
        qc.invalidateQueries({ queryKey: ['regime-mainline'] }),
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
          <h1 className="text-[16px] leading-6 font-semibold text-foreground">市场环境</h1>
          <span className="text-xs text-muted">
            {view === 'phase' ? '涨停情绪 · 市场阶段 · 主线脉络' : '每日环境状态 · 赚钱效应 · 趋势分析'}
          </span>
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

      {/* ── 视图切换: 市场环境 / 情绪周期 (两组内容 tab 隔离, 减少单页高度) ── */}
      <div className="flex items-center gap-2">
        <div className="flex items-center rounded-btn border border-border bg-base/60 p-0.5">
          {([['regime', '市场环境', Activity], ['phase', '情绪周期', Flame]] as const).map(([k, label, Icon]) => (
            <button key={k} onClick={() => setView(k)}
              className={cn('inline-flex items-center gap-1.5 h-7 rounded-[5px] px-3 text-xs font-medium transition-colors',
                view === k ? 'bg-accent text-white shadow-sm' : 'text-secondary hover:text-foreground')}>
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-muted">两组内容同页切换 · 共用时间范围</span>
      </div>

      {/* ══ 情绪周期 tab: 阶段概览 + 时间轴 + 阶段×主线 + 主线排行 ══ */}
      <div className={cn('space-y-4', view !== 'phase' && 'hidden')}>

      {/* ── 市场阶段概览 (情绪周期 + 梯队指标 + 当前主线) ── */}
      {hasPhaseData && latest ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {/* 当前阶段 */}
          <div className={cn(cardCls, 'p-3')}>
            <div className="flex items-center gap-1.5 text-[10px] text-muted">
              <Flame className="h-3 w-3" /> 当前阶段 · {latest.date}
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span
                className="text-2xl font-bold cursor-help"
                style={{ color: MARKET_PHASE_COLORS[phaseStreak?.phase ?? 'repair'] }}
                title={phaseStreak ? `${MARKET_PHASE_GUIDE[phaseStreak.phase].meaning}\n应对: ${MARKET_PHASE_GUIDE[phaseStreak.phase].action}` : undefined}
              >
                {MARKET_PHASE_LABELS[phaseStreak?.phase ?? 'repair']}
              </span>
              {phaseStreak && <span className="text-xs text-muted">第 {phaseStreak.streak} 天</span>}
            </div>
            {phaseStreak && (
              <div className="mt-0.5 text-[9px] text-muted/90 truncate" title={MARKET_PHASE_GUIDE[phaseStreak.phase].meaning}>
                {MARKET_PHASE_GUIDE[phaseStreak.phase].action}
              </div>
            )}
            <div className="mt-1.5 flex flex-wrap gap-1">
              {latestMainlines.length > 0 ? latestMainlines.map(m => (
                <span key={m.member} className="rounded px-1.5 py-px text-[9px] font-medium"
                  style={{ color: '#f59e0b', backgroundColor: '#f59e0b18' }} title={`涨停${m.limit_up_count}家 · 最高${m.max_boards}板 · 梯队${m.rungs_filled}档`}>
                  {m.member}
                </span>
              )) : <span className="text-[9px] text-muted">暂无主线数据</span>}
            </div>
          </div>
          {([
            { label: '市场高度', val: latest.max_consecutive, unit: '板', color: '#ef4444', field: 'max_consecutive' as const },
            { label: '首板宽度', val: latest.first_board, unit: '家', color: '#f97316', field: 'first_board' as const },
            { label: '2板+宽度', val: latest.ge2_count, unit: '家', color: '#f59e0b', field: 'ge2_count' as const },
            { label: '晋级率', val: latest.promo_rate != null ? `${(latest.promo_rate * 100).toFixed(0)}%` : '—',
              unit: '', color: '#3b82f6', field: 'promo_rate' as const,
              sub: latest.promo_pool != null ? `池 ${latest.promo_pool} 家` : undefined },
            { label: '梯队完整度', val: latest.ladder_completeness != null ? `${(latest.ladder_completeness * 100).toFixed(0)}%` : '—',
              unit: '', color: '#a855f7', field: 'ladder_completeness' as const, sub: '2板→最高板不断档' },
          ] as { label: string; val: React.ReactNode; unit: string; color: string; field: keyof RegimeRow; sub?: string }[]).map(k => {
            const p = pctRank(k.field)
            return (
              <div key={k.label} className={cn(cardCls, 'p-3')}>
                <div className="flex items-center gap-1.5 text-[10px] text-muted">
                  <Activity className="h-3 w-3" /> {k.label}
                  {p != null && (
                    <span
                      className="ml-auto font-mono text-[9px] text-muted/70"
                      title={`当前值在所选时间窗口内的历史分位 (p${p})`}
                    >
                      p{p}
                    </span>
                  )}
                </div>
                <div className="mt-1.5 text-2xl font-bold" style={{ color: k.color }}>
                  {k.val}<span className="ml-0.5 text-xs font-normal text-muted">{k.unit}</span>
                </div>
                {k.sub && <div className="mt-1 text-[9px] text-muted">{k.sub}</div>}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="rounded-card border border-dashed border-border p-4 text-center text-xs text-muted">
          市场阶段(情绪周期)数据尚未生成 — 点击右上角「重算」即可回填全部历史阶段与主线
        </div>
      )}

      {/* ── 阶段规律: 当前阶段的历史统计 + 下阶段转移分布 (窗口内 segments 提炼) ── */}
      {phaseStats && phaseStreak && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Flame} title="阶段规律"
            hint="当前阶段在窗口内的历史统计 · 去向=每段之后进入的阶段" />
          <div className="mt-2 flex flex-wrap items-center gap-x-6 gap-y-2 text-[11px]">
            <span className="text-secondary">
              <span style={{ color: MARKET_PHASE_COLORS[phaseStreak.phase], fontWeight: 600 }}>
                {MARKET_PHASE_LABELS[phaseStreak.phase]}
              </span>
              <span className="text-muted"> 已持续 </span>
              <span className="font-mono text-foreground">{phaseStreak.streak}</span> 天
              <span className="text-muted"> · 历史 {phaseStats.count} 段, 平均 {phaseStats.avgDays} 天, 最长 {phaseStats.maxDays} 天</span>
            </span>
            {phaseStreak.streak > phaseStats.avgDays && (
              <span className="rounded bg-warning/10 px-1.5 py-px text-[10px] font-medium text-warning"
                title="持续天数已超过窗口内该阶段的平均时长">
                已超历史平均
              </span>
            )}
            {phaseStats.transitions.length > 0 && (
              <span className="flex flex-wrap items-center gap-1.5 text-muted">
                历史去向:
                {phaseStats.transitions.map(t => (
                  <span key={t.phase} className="rounded px-1.5 py-px font-medium"
                    style={{ color: MARKET_PHASE_COLORS[t.phase], backgroundColor: MARKET_PHASE_COLORS[t.phase] + '18' }}
                    title={`${phaseStats.count} 段中 ${t.count} 段之后进入`}>
                    {MARKET_PHASE_LABELS[t.phase]} {t.pct}%
                  </span>
                ))}
              </span>
            )}
          </div>
        </div>
      )}

      {/* ── 情绪周期时间轴 (阶段色带 + 高度/宽度/晋级率 + 点击回看) ── */}
      {hasPhaseData && rows.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Flame} title="情绪周期时间轴"
            hint="堆叠柱=涨停(首板+2板+) · 高度(红) · 晋级率(蓝) · ▲切换 · 点击日期回看当日" />
          {/* 当日快照: 点击图表日期出现 — 阶段/梯队指标/当日主线 top3 一屏回看 */}
          {selRow && (
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-accent/25 bg-accent/5 px-2.5 py-1.5">
              <span className="flex items-center gap-1.5">
                <span className="font-mono text-xs font-semibold text-foreground">{selRow.date}</span>
                <span className="rounded px-1.5 py-px text-[10px] font-semibold"
                  style={{ color: MARKET_PHASE_COLORS[selRow.phase as MarketPhase], backgroundColor: MARKET_PHASE_COLORS[selRow.phase as MarketPhase] + '20' }}>
                  {MARKET_PHASE_LABELS[selRow.phase as MarketPhase]}
                </span>
                {selStreak != null && <span className="text-[10px] text-muted">第 {selStreak} 天</span>}
              </span>
              <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1 font-mono text-[10px] text-secondary">
                <span>高度 <b className="text-danger">{selRow.max_consecutive}板</b></span>
                <span>涨停 <b className="text-foreground">{selRow.limit_up ?? '—'}</b>
                  <span className="text-muted"> = 首板{selRow.first_board ?? '—'} + 2板+{selRow.ge2_count ?? '—'}</span></span>
                <span>晋级 <b className="text-[#3b82f6]">{selRow.promo_rate != null ? (selRow.promo_rate * 100).toFixed(0) + '%' : '—'}</b></span>
                <span>封板 <b className="text-[#10b981]">{selRow.seal_rate != null ? (selRow.seal_rate * 100).toFixed(0) + '%' : '—'}</b></span>
              </span>
              <span className="flex flex-wrap items-center gap-1">
                {selMainlines.length > 0 ? selMainlines.map(m => (
                  <span key={m.member} className="rounded px-1.5 py-px text-[9px] font-medium"
                    style={{ color: '#f59e0b', backgroundColor: '#f59e0b18' }}
                    title={`涨停${m.limit_up_count}家 · 最高${m.max_boards}板 · 梯队${m.rungs_filled}档`}>
                    {m.member}
                  </span>
                )) : <span className="text-[9px] text-muted">当日无主线数据</span>}
              </span>
              <button onClick={() => setSelDate(null)}
                className="ml-auto flex items-center gap-1 rounded px-1.5 py-px text-[10px] text-muted hover:bg-elevated hover:text-foreground">
                <X className="h-3 w-3" /> 回到今日
              </button>
            </div>
          )}
          <div ref={phaseChartRef} className="mt-2 h-[280px]" />
          <div className="mt-1.5 flex h-6 w-full overflow-hidden rounded-md">
            {rows.map(r => (
              <div key={r.date} title={`${r.date} ${MARKET_PHASE_LABELS[r.phase as MarketPhase]}`}
                className="flex-1 min-w-[2px] transition-opacity hover:opacity-80"
                style={{ backgroundColor: MARKET_PHASE_COLORS[r.phase as MarketPhase] }} />
            ))}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted">
            {MARKET_PHASE_ORDER.map(p => (
              <span key={p} className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded" style={{ backgroundColor: MARKET_PHASE_COLORS[p] }} />
                {MARKET_PHASE_LABELS[p]}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ── 阶段 × 主线 (什么阶段走什么主升) ── */}
      {segments.length > 0 && (
        <div className={cn(cardCls, 'p-3')}>
          <SectionTitle icon={Layers} title="阶段 × 主线"
            hint={`${segments.length} 段 · 主线按段内 top5 天数排序`} />
          {/* 最大高度内滚动: 段数多时不再拉长页面; 表头吸顶保证滚动时列名可见。
              border-separate 是 sticky 前提 — Chromium 在 border-collapse:collapse
              (preflight 默认) 下表格元素 sticky 失效; spacing-0 保持视觉不变。 */}
          <div className="mt-2 max-h-[420px] overflow-auto">
            <table className="w-full min-w-[760px] border-separate border-spacing-0 text-left text-[11px]">
              <thead>
                <tr className="text-[10px] text-muted">
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium">阶段</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium">区间</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium text-right">天数</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium text-right">高度</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium text-right">2板+</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium text-right">晋级率</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 pr-3 font-medium text-right">封板率</th>
                  <th className="sticky top-0 z-10 border-b border-border bg-surface py-1.5 font-medium">主导主线</th>
                </tr>
              </thead>
              <tbody>
                {[...segments].reverse().map((seg, i) => (
                  <tr key={`${seg.start}-${seg.phase}-${i}`}>
                    <td className="border-b border-border/50 py-1.5 pr-3">
                      <span className="rounded px-1.5 py-px text-[10px] font-semibold"
                        style={{ color: MARKET_PHASE_COLORS[seg.phase], backgroundColor: MARKET_PHASE_COLORS[seg.phase] + '20' }}>
                        {seg.label}
                      </span>
                    </td>
                    <td className="border-b border-border/50 py-1.5 pr-3 font-mono text-[10px] text-secondary">
                      {seg.start.slice(5)} ~ {seg.end.slice(5)}
                    </td>
                    <td className="border-b border-border/50 py-1.5 pr-3 text-right font-mono">{seg.days}</td>
                    <td className="border-b border-border/50 py-1.5 pr-3 text-right font-mono">{seg.avg_height}</td>
                    <td className="border-b border-border/50 py-1.5 pr-3 text-right font-mono">{seg.avg_ge2}</td>
                    <td className="border-b border-border/50 py-1.5 pr-3 text-right font-mono">
                      {seg.avg_promo != null ? `${(seg.avg_promo * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="border-b border-border/50 py-1.5 pr-3 text-right font-mono">
                      {seg.avg_seal_rate != null ? `${(seg.avg_seal_rate * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="border-b border-border/50 py-1.5">
                      <div className="flex flex-wrap gap-1">
                        {seg.top_mainlines.length > 0 ? seg.top_mainlines.map(m => (
                          <span key={m.member} className="rounded px-1.5 py-px text-[9px]"
                            style={{ color: '#f59e0b', backgroundColor: '#f59e0b18' }}
                            title={`top5 ${m.top5_days} 天 · 最高 ${m.max_boards} 板 · 龙头 ${m.leader_symbol}`}>
                            {m.member}<span className="ml-1 font-mono opacity-70">{m.top5_days}d</span>
                          </span>
                        )) : <span className="text-[9px] text-muted">—</span>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── 主线排行 (窗口内持续性 + 过滤设置) ── */}
      <div className={cn(cardCls, 'p-3')}>
        <SectionTitle icon={Layers} title="主线排行"
          hint={
            <span className="flex items-center gap-2">
              <span className="hidden sm:inline text-[9px] text-muted">{mainline.data?.membership_note}</span>
              <button
                onClick={() => setFilterOpen(v => !v)}
                className={cn('inline-flex items-center gap-1 rounded-btn border px-2 py-0.5 text-[10px] transition-colors',
                  filterOpen ? 'border-accent/50 text-accent' : 'border-border bg-base text-secondary hover:text-accent')}
              >
                <Filter className="h-3 w-3" /> 过滤
              </button>
            </span>
          }
        />
        <div className="mt-2 flex items-center gap-2">
          <div className="flex items-center rounded-btn border border-border bg-base/60 p-0.5">
            {([['concept', '概念'], ['industry', '行业']] as const).map(([k, label]) => (
              <button key={k} onClick={() => setMainlineKind(k)}
                className={cn('h-6 rounded-[5px] px-2.5 text-xs font-medium transition-colors',
                  mainlineKind === k ? 'bg-accent text-white shadow-sm' : 'text-secondary hover:text-foreground')}>
                {label}
              </button>
            ))}
          </div>
          <span className="text-[10px] text-muted">窗口内 top1 天数排序 · 点击「过滤」配置宽基概念屏蔽</span>
        </div>
        {filterOpen && (
          <MainlineFilterPanel
            filter={mainline.data?.filter}
            onDone={async () => {
              await qc.invalidateQueries({ queryKey: ['regime-mainline'] })
              await qc.invalidateQueries({ queryKey: ['regime-phases'] })
            }}
          />
        )}
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-[11px]">
            <thead>
              <tr className="border-b border-border text-[10px] text-muted">
                <th className="py-1.5 pr-3 font-medium">#</th>
                <th className="py-1.5 pr-3 font-medium">主线</th>
                <th className="py-1.5 pr-3 font-medium text-right">top1 天数</th>
                <th className="py-1.5 pr-3 font-medium text-right">日均分</th>
                <th className="py-1.5 pr-3 font-medium text-right">最高板</th>
              </tr>
            </thead>
            <tbody>
              {(mainline.data?.leaders ?? []).map((l, i) => (
                <tr key={l.member} className="border-b border-border/50 last:border-0">
                  <td className="py-1.5 pr-3 font-mono text-muted">{i + 1}</td>
                  <td className="py-1.5 pr-3 font-medium text-foreground">{l.member}</td>
                  <td className="py-1.5 pr-3 text-right font-mono">{l.top1_days}</td>
                  <td className="py-1.5 pr-3 text-right font-mono">{l.avg_score}</td>
                  <td className="py-1.5 pr-3 text-right font-mono">{l.max_boards} 板</td>
                </tr>
              ))}
              {(mainline.data?.leaders ?? []).length === 0 && (
                <tr><td colSpan={5} className="py-4 text-center text-[10px] text-muted">
                  {mainline.isLoading ? '加载中…' : '暂无主线数据 — 点击「重算」回填, 或检查过滤设置'}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      </div>{/* /情绪周期 tab */}

      {/* ══ 市场环境 tab: 最新日概览 + 状态时间轴 + 趋势/分布 + 日历热力图 ══ */}
      <div className={cn('space-y-4', view !== 'regime' && 'hidden')}>

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

      </div>{/* /市场环境 tab */}

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

// ── 主线过滤设置面板 ──────────────────────────────────────
// 宽基/风格标签(融资融券/沪深股通等数千成分)会霸占主线榜首。默认按成员数
// 上限过滤; 用户可调阈值并按名称屏蔽特定概念, 保存后自动重算主线。
// ST 剔除开关联动情绪周期口径 — 切换时额外触发 regime 全量重算。
function MainlineFilterPanel({ filter, onDone }: {
  filter: { min_members: number; max_members: number; blacklist: string[]; exclude_st?: boolean } | undefined
  onDone: () => Promise<void>
}) {
  const [minMembers, setMinMembers] = useState(String(filter?.min_members ?? 4))
  const [maxMembers, setMaxMembers] = useState(String(filter?.max_members ?? 600))
  const [blacklist, setBlacklist] = useState<string[]>(filter?.blacklist ?? [])
  const [excludeSt, setExcludeSt] = useState(filter?.exclude_st ?? true)
  const [input, setInput] = useState('')
  const [saving, setSaving] = useState(false)

  const addTag = () => {
    const v = input.trim()
    if (v && !blacklist.includes(v)) setBlacklist([...blacklist, v])
    setInput('')
  }

  const save = async () => {
    setSaving(true)
    try {
      await api.mainlineFilterUpdate({
        min_members: Math.max(1, Number(minMembers) || 4),
        max_members: Math.max(50, Number(maxMembers) || 600),
        blacklist,
        exclude_st: excludeSt,
      })
      const stChanged = excludeSt !== (filter?.exclude_st ?? true)
      if (stChanged) {
        // 口径切换影响情绪周期驱动指标, 需全量重算 regime+主线(较重, 需等待)
        await api.regimeRecompute()
        toast('过滤已保存, 主线与情绪周期已全量重算', 'success')
      } else {
        await api.regimeMainlineRecompute()
        toast('过滤已保存, 主线已重算', 'success')
      }
      await onDone()
    } catch (e) {
      toast(`保存失败 · ${String((e as Error)?.message || e)}`, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mt-2 rounded-btn border border-border bg-base/40 p-2.5">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] text-muted">成员数上限(过滤宽基标签)</span>
          <input type="number" min={50} max={5000} value={maxMembers}
            onChange={e => setMaxMembers(e.target.value)}
            className="h-7 w-24 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] text-muted">成员数下限</span>
          <input type="number" min={1} max={200} value={minMembers}
            onChange={e => setMinMembers(e.target.value)}
            className="h-7 w-20 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
        </label>
        <div className="flex min-w-[220px] flex-1 flex-col gap-1">
          <span className="text-[10px] text-muted">按名称屏蔽(回车添加)</span>
          <div className="flex items-center gap-1.5">
            <input value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTag() } }}
              placeholder="如: 融资融券、沪股通"
              className="h-7 flex-1 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
          </div>
          {blacklist.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {blacklist.map(b => (
                <span key={b} className="inline-flex items-center gap-1 rounded bg-accent/10 px-1.5 py-px text-[10px] text-accent">
                  {b}
                  <button onClick={() => setBlacklist(blacklist.filter(x => x !== b))} className="hover:text-foreground">
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
        <button onClick={save} disabled={saving}
          className="h-7 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50">
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '保存并重算'}
        </button>
      </div>
      <div className="mt-2 flex items-center gap-2 border-t border-border/60 pt-2">
        <button
          type="button"
          role="switch"
          aria-checked={excludeSt}
          aria-label="统计剔除 ST 股"
          onClick={() => setExcludeSt(v => !v)}
          className={cn(
            'relative inline-flex h-4.5 w-8 items-center rounded-full border transition-all duration-200',
            excludeSt ? 'border-accent/50 bg-accent' : 'border-border bg-elevated hover:border-muted',
          )}
        >
          <span className={cn(
            'inline-block h-3 w-3 rounded-full border border-black/5 bg-white shadow-sm transition-transform duration-200',
            excludeSt ? 'translate-x-[17px]' : 'translate-x-0.5',
          )} />
        </button>
        <span className="text-[11px] text-secondary">
          统计剔除 ST 股
          <span className="ml-1.5 text-[9px] text-muted">主线 + 情绪周期统一口径; 切换后自动全量重算(约 1-2 分钟)</span>
        </span>
      </div>
      <div className="mt-1.5 text-[9px] text-muted">
        说明: 成分股数超过上限的概念(如 融资融券~7700家/沪深股通~3300家)视为宽基/风格标签, 不参与主线排名;
        风险警示股(名称含 ST)不参与涨停梯队统计 — 主板 5% 便宜板时代曾系统性霸榜, 且 ST 是状态桶而非题材。修改后自动重算全部历史(秒级~分钟级)。
      </div>
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
