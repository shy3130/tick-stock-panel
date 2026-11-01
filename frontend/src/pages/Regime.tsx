/**
 * 市场环境(Regime)页 — 每日环境状态时序趋势 + 状态分布。
 *
 * 数据来源: 后端 regime_builder 批算的时序表(每日离散状态 + 多维指标)。
 * 不复刻 Dashboard 的当日总览(那是单日快照), 聚焦历史趋势与状态分布。
 *
 * 时间范围: 1年(250交易日) / 2年(500) / 自定义(1~1000天) / 全部(走日期范围)。
 * 沿用全站主题与状态语义色, 按当前判断、驱动指标、历史验证组织信息。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
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
import { useChartTheme, useTheme } from '@/lib/theme'
import { toast } from '@/components/Toast'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'
import './Regime.css'

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
  onReady?: (inst: echarts.ECharts | null) => void,
) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)
  const appliedOptionRef = useRef<echarts.EChartsOption | null>(null)
  useEffect(() => {
    const onResize = () => instRef.current?.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      observerRef.current?.disconnect()
      instRef.current?.dispose()
      instRef.current = null
    }
  }, [])
  useEffect(() => {
    if (!ref.current) {
      observerRef.current?.disconnect()
      if (instRef.current) {
        instRef.current.dispose()
        instRef.current = null
        appliedOptionRef.current = null
        onReady?.(null)
      }
      return
    }
    if (instRef.current && instRef.current.getDom() !== ref.current) {
      observerRef.current?.disconnect()
      instRef.current.dispose()
      instRef.current = null
      appliedOptionRef.current = null
    }
    // 惰性 init: 图表容器可能条件渲染晚于组件挂载 (如情绪周期图依赖异步查询结果,
    // 冷加载时首帧 rows 为空 → div 不在 DOM, 仅挂载时跑一次的 init 会扑空)。
    // 数据到达后 option 变化触发本 effect, 此时 div 已挂载 — 补建实例再 setOption。
    if (!instRef.current) {
      instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
      appliedOptionRef.current = null
      if (typeof ResizeObserver !== 'undefined') {
        observerRef.current = new ResizeObserver(() => instRef.current?.resize())
        observerRef.current.observe(ref.current)
      }
      onReady?.(instRef.current)
    }
    if (option) {
      if (appliedOptionRef.current !== option) {
        instRef.current.setOption(option, { notMerge: true })
        appliedOptionRef.current = option
      }
      // 容器可能经历 display:none(tab 隐藏) → 可见的切换, 画布尺寸需要按当前
      // 容器实际尺寸重算; 调用方把 view 等显隐依赖传入 deps 以触发本 effect。
      instRef.current.resize()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [option, ...deps])
  return ref
}

// ── 页内标题 ──────────────────────────────────────────────
function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="regime-section-heading">
      <div className="regime-section-title"><Icon aria-hidden="true" /><h2>{title}</h2></div>
      {hint != null && <div className="regime-section-hint">{hint}</div>}
    </div>
  )
}

const cardCls = 'regime-panel'

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
  const theme = useTheme()

  // coverage: "全部"模式 + 标题展示依赖
  const coverage = useQuery({
    queryKey: QK.regimeCoverage,
    queryFn: () => api.regimeCoverage(),
    staleTime: 5 * 60 * 1000,
  })

  const days = resolveDays(range, coverage.data)
  const histRange = resolveHistoryRange(range, coverage.data)

  // queryKey 用实际请求三元组 (QK 集中定义): 'all' 在 coverage 到达前后参数
  // 不同会自动换键重拉; 补算后经裸前缀 regimeHistoryPrefix 失效所有变体
  const history = useQuery({
    queryKey: QK.regimeHistory(histRange),
    queryFn: () => api.regimeHistory(histRange.start, histRange.end, histRange.limit),
    staleTime: 5 * 60 * 1000,
    // '全部' 依赖 coverage 的真实日期范围: 到达前不发请求 (先发会把后端默认
    // 窗口数据短暂当成"全部"渲染), coverage 失败时兜底放行避免查询永久禁用
    enabled: range !== 'all' || !!coverage.data || !!coverage.error,
  })
  const states = useQuery({
    queryKey: QK.regimeStates(days),
    queryFn: () => api.regimeStates(days),
    staleTime: 5 * 60 * 1000,
  })
  const rows: RegimeRow[] = history.data?.rows ?? []
  // 年数/自定义窗口通过 limit 拉取, 阶段与主线接口只接受日期边界。
  // 用已返回交易日收口, 避免主图为近一年、统计却混入全部历史。
  const detailStart = rows[0]?.date
  const detailEnd = rows[rows.length - 1]?.date
  // 情绪周期阶段段 + 主线排行(与 history 同一时间范围)
  const phases = useQuery({
    queryKey: QK.regimePhases(detailStart, detailEnd),
    queryFn: () => api.regimePhases(detailStart, detailEnd),
    staleTime: 5 * 60 * 1000,
    enabled: rows.length > 0,
  })
  const [mainlineKind, setMainlineKind] = useState<'concept' | 'industry'>('concept')
  const [filterOpen, setFilterOpen] = useState(false)
  // 时间轴点击选中的交易日 (当日快照联动); null = 未选。窗口切换后失效。
  const [selDate, setSelDate] = useState<string | null>(null)
  useEffect(() => { setSelDate(null) }, [histRange.start, histRange.end, histRange.limit])
  const mainline = useQuery({
    queryKey: QK.regimeMainline(mainlineKind, detailStart, detailEnd),
    queryFn: () => api.regimeMainline(detailStart, detailEnd, 10, mainlineKind),
    staleTime: 5 * 60 * 1000,
    enabled: rows.length > 0,
  })
  const [recomputing, setRecomputing] = useState(false)

  const latest = rows.length > 0 ? rows[rows.length - 1] : null
  const hasPhaseData = rows.length > 0 && rows.some(r => r.phase != null)
  const segments = rows.length > 0 ? phases.data?.segments ?? [] : []

  // 当前阶段持续天数(末尾连续同阶段) + 当前主线(最新交易日 top3)
  const phaseStreak = useMemo(() => {
    if (!hasPhaseData) return null
    const lastPhase = rows[rows.length - 1].phase
    if (!lastPhase) return null
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
              itemStyle: { color, opacity: 0.045 },
              label: {
                show: bandDays >= 6,
                formatter: MARKET_PHASE_LABELS[prevPhase as MarketPhase],
                position: 'insideTopLeft', distance: 6,
                color: ct.text, fontSize: 11, fontWeight: 600,
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
        trigger: 'axis', confine: true, backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder,
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
        type: 'scroll', itemWidth: 12, itemHeight: 7, itemGap: 16, inactiveColor: ct.text,
        data: ['首板', '2板+', '高度', '晋级率', '封板率'],
        selected: { 封板率: false },
        textStyle: { color: ct.text, fontSize: 11 }, top: 0,
      },
      grid: { left: 40, right: 44, top: 42, bottom: 52 },
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
        { type: 'slider', bottom: 6, height: 20, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        // 首板/2板+ 堆叠柱: 两段之和 = 当日涨停总数 (首板宽度=底部, 2板+=顶段),
        // 总高看涨停宽度、顶段看连板厚度; 高度线在其上穿行
        { name: '首板', type: 'bar', stack: 'lu', data: firstBoard, yAxisIndex: 0,
          barMaxWidth: 5, itemStyle: { color: theme === 'dark' ? '#f97316' : '#b45309', opacity: theme === 'dark' ? 0.4 : 0.85 }, z: 1 },
        { name: '2板+', type: 'bar', stack: 'lu', data: ge2, yAxisIndex: 0,
          barMaxWidth: 5, itemStyle: { color: theme === 'dark' ? '#f59e0b' : '#d97706', opacity: theme === 'dark' ? 0.7 : 1 }, z: 1 },
        { name: '高度', type: 'line', itemStyle: { color: '#ef4444' }, data: heights, smooth: true, symbol: 'none', yAxisIndex: 0,
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
        { name: '晋级率', type: 'line', itemStyle: { color: '#3b82f6' }, data: promo, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.2, color: '#3b82f6', type: 'dotted' }, z: 2 },
        { name: '封板率', type: 'line', itemStyle: { color: '#10b981' }, data: seal, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.2, color: '#10b981', type: 'dashed' }, z: 2 },
      ],
    }
  }, [rows, days, ct, theme, hasPhaseData, selDate])
  const [phaseChartInst, setPhaseChartInst] = useState<echarts.ECharts | null>(null)
  const phaseChartRef = useEChart(phaseOption, [phaseOption, view], setPhaseChartInst)
  // 点击图表任意位置 → 选中最近的交易日 (zrender 级监听, 命中区为整个网格,
  // 不依赖细线/窄柱的精确点击); 点击图例/dataZoom 不在网格内, 自动忽略
  useEffect(() => {
    if (!phaseChartInst || phaseChartInst.isDisposed() || !phaseOption) return
    const zr = phaseChartInst.getZr()
    if (!zr) return
    const onClick = (e: { offsetX: number; offsetY: number }) => {
      if (!phaseChartInst.containPixel('grid', [e.offsetX, e.offsetY])) return
      const dates = rows.map(r => r.date)
      const idx = Math.round(phaseChartInst.convertFromPixel({ seriesIndex: 0 }, [e.offsetX, e.offsetY])[0])
      if (Number.isFinite(idx) && idx >= 0 && idx < dates.length) setSelDate(dates[idx])
    }
    zr.on('click', onClick)
    return () => { if (!phaseChartInst.isDisposed()) zr.off('click', onClick) }
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
            { xAxis: bandStart, itemStyle: { color: REGIME_STATE_COLORS[prevState as RegimeState], opacity: 0.025 } },
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
      tooltip: { trigger: 'axis', confine: true, backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText } },
      legend: {
        type: 'scroll', itemWidth: 12, itemHeight: 7, itemGap: 16, inactiveColor: ct.text,
        data: ['综合分', '涨停数', '赚钱', '投机', '抗跌', '趋势'],
        textStyle: { color: ct.text, fontSize: 11 }, top: 0,
        // 综合分与低强调涨停柱同屏, 四维指标可按需叠加
        selected: { '综合分': true, '涨停数': true, '赚钱': false, '投机': false, '抗跌': false, '趋势': false },
      },
      grid: { left: 40, right: 68, top: 44, bottom: 56 },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLabel: { color: ct.text, fontSize: 11, formatter: (v: string) => v.slice(5) },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      yAxis: [
        { type: 'value', name: '综合分', min: 0, max: 100, position: 'left', axisLabel: { color: ct.text, fontSize: 11 }, splitLine: { lineStyle: { color: ct.grid } }, nameTextStyle: { color: ct.text } },
        { type: 'value', position: 'right', axisLabel: { color: ct.text, fontSize: 11, formatter: '{value}家' }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - (60 / days) * 100) },
        { type: 'slider', bottom: 8, height: 20, borderColor: ct.border, fillerColor: ct.zoomFill, textStyle: { color: ct.text } },
      ],
      series: [
        // 涨停数柱状(辅助右轴, 单位为家)
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 1, barMaxWidth: 6,
          itemStyle: { color: REGIME_STATE_COLORS.strong, opacity: 0.22 }, z: 1 },
        // 4 子维度与综合分共用左轴 0–100, 点图例可切换。
        { name: '赚钱', type: 'line', itemStyle: { color: '#f59e0b' }, data: profit, smooth: true, symbol: 'none', yAxisIndex: 0,
          lineStyle: { ...subLineStyle, color: '#f59e0b' }, z: 2 },
        { name: '投机', type: 'line', itemStyle: { color: '#a855f7' }, data: speculation, smooth: true, symbol: 'none', yAxisIndex: 0,
          lineStyle: { ...subLineStyle, color: '#a855f7' }, z: 2 },
        { name: '抗跌', type: 'line', itemStyle: { color: '#10b981' }, data: resilience, smooth: true, symbol: 'none', yAxisIndex: 0,
          lineStyle: { ...subLineStyle, color: '#10b981' }, z: 2 },
        { name: '趋势', type: 'line', itemStyle: { color: '#3b82f6' }, data: trend, smooth: true, symbol: 'none', yAxisIndex: 0,
          lineStyle: { ...subLineStyle, color: '#3b82f6' }, z: 2 },
        // 综合分主线 + 状态背景色带 + 阈值横虚线
        { name: '综合分', type: 'line', data: scores, smooth: true, symbol: 'none', yAxisIndex: 0,
          itemStyle: { color: ct.textStrong }, lineStyle: { width: 2.2, color: ct.textStrong }, areaStyle: { color: ct.textStrong, opacity: 0.035 }, z: 3,
          markArea: { silent: true, data: stateBands },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { type: 'dashed', width: 1 },
            label: { position: 'insideEndTop', fontSize: 10, color: ct.textStrong, padding: [2, 4] },
            data: [
              { yAxis: 70, lineStyle: { color: REGIME_STATE_COLORS.strong, opacity: 0.55 },
                label: { formatter: '强势 70', color: ct.textStrong, backgroundColor: REGIME_STATE_COLORS.strong + '20', borderRadius: 3 } },
              { yAxis: 55, lineStyle: { color: REGIME_STATE_COLORS.lean_strong, opacity: 0.55 },
                label: { formatter: '偏强 55', color: ct.textStrong, backgroundColor: REGIME_STATE_COLORS.lean_strong + '20', borderRadius: 3 } },
              { yAxis: 45, lineStyle: { color: REGIME_STATE_COLORS.range, opacity: 0.55 },
                label: { formatter: '震荡 45', color: ct.textStrong, backgroundColor: REGIME_STATE_COLORS.range + '20', borderRadius: 3 } },
              { yAxis: 30, lineStyle: { color: REGIME_STATE_COLORS.lean_weak, opacity: 0.55 },
                label: { formatter: '偏弱 30', color: ct.textStrong, backgroundColor: REGIME_STATE_COLORS.lean_weak + '20', borderRadius: 3 } },
            ],
          } },
      ],
    }
  }, [rows, days, ct])
  const trendRef = useEChart(trendOption, [trendOption, view])

  const distribution = STATE_ORDER.map(state => states.data?.distribution.find(d => d.state === state))
    .filter((item): item is NonNullable<typeof item> => !!item)
  const distributionTotal = distribution.reduce((sum, item) => sum + item.count, 0)
  const distributionOption = useMemo<echarts.EChartsOption | null>(() => {
    const items = states.data?.distribution ?? []
    if (!items.some(item => item.count > 0)) return null
    return {
      backgroundColor: 'transparent', animation: false,
      tooltip: { trigger: 'item', confine: true, backgroundColor: ct.tooltipBg, borderColor: ct.tooltipBorder, textStyle: { color: ct.tooltipText }, formatter: '{b}：{c} 天（{d}%）' },
      series: [{
        type: 'pie', radius: ['60%', '81%'], center: ['50%', '50%'],
        label: { show: false }, labelLine: { show: false },
        itemStyle: { borderColor: ct.tooltipBg, borderWidth: 2, borderRadius: 2 },
        emphasis: { scaleSize: 3 },
        data: STATE_ORDER.map(state => items.find(item => item.state === state)).filter((item): item is NonNullable<typeof item> => !!item)
          .map(item => ({ name: item.label, value: item.count, itemStyle: { color: REGIME_STATE_COLORS[item.state] } })),
      }],
    }
  }, [states.data, ct])
  const distributionRef = useEChart(distributionOption, [view, rows.length])

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
    <div className="regime-page">
      <header className="regime-heading">
        <div className="regime-heading-copy">
          <Activity className="regime-heading-icon" aria-hidden="true" /><h1>市场环境</h1>
          <p>
            {view === 'phase' ? '涨停情绪 · 市场阶段 · 主线脉络' : '每日环境状态 · 赚钱效应 · 趋势分析'}
          </p>
        </div>
          <div className="regime-toolbar">
            <div className="regime-range" role="group" aria-label="时间范围">
              {(['1y', '2y', 'all'] as const).map(k => (
                <button
                  key={k}
                  type="button"
                  onClick={() => setRange(k)}
                  aria-pressed={isPresetKey(range, k)}
                >
                  {RANGE_LABEL[k]}
                </button>
              ))}
              <button
                type="button"
                onClick={() => setCustomOpen(true)}
                aria-pressed={typeof range === 'object'}
                className="inline-flex items-center justify-center gap-1"
              >
                {typeof range === 'object' && <Pencil className="h-3 w-3" />}
                {customLabel}
              </button>
            </div>
            {/* 重算 */}
            <button type="button" onClick={handleRecompute} disabled={recomputing} className="regime-action">
              {recomputing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              {recomputing ? '重算中…' : '重算'}
            </button>
          </div>
      </header>

      {/* ── 视图切换: 市场环境 / 情绪周期 (两组内容 tab 隔离, 减少单页高度) ── */}
      <div className="regime-tabbar">
        <div className="regime-tabs" role="tablist" aria-label="市场环境视图">
          {([['regime', '市场环境', Activity], ['phase', '情绪周期', Flame]] as const).map(([k, label, Icon]) => (
            <button key={k} id={`regime-tab-${k}`} type="button" role="tab"
              aria-selected={view === k} aria-controls={`regime-panel-${k}`} tabIndex={view === k ? 0 : -1}
              onClick={() => setView(k)}
              onKeyDown={event => {
                const next = event.key === 'Home' ? 'regime' : event.key === 'End' ? 'phase'
                  : ['ArrowLeft', 'ArrowRight'].includes(event.key) ? (k === 'regime' ? 'phase' : 'regime') : null
                if (next) {
                  event.preventDefault()
                  setView(next)
                  document.getElementById(`regime-tab-${next}`)?.focus()
                }
              }}>
              <Icon aria-hidden="true" />
              {label}
            </button>
          ))}
        </div>
        <span className="regime-window">
          {rows.length > 0 ? `${rows[0].date} — ${latest!.date} · ${rows.length} 个交易日` : '按所选时间范围查看历史'}
        </span>
      </div>

      {/* ══ 情绪周期 tab: 阶段概览 + 时间轴 + 阶段×主线 + 主线排行 ══ */}
      <div className="regime-view-panel" id="regime-panel-phase" role="tabpanel" aria-labelledby="regime-tab-phase" hidden={view !== 'phase'}>

      {/* ── 市场阶段概览 (情绪周期 + 梯队指标 + 当前主线) ── */}
      {phaseStreak && latest ? (
        <div className="regime-phase-summary">
          <section className={cn(cardCls, 'regime-phase-current')} aria-label="当前情绪阶段" style={{ '--metric-color': MARKET_PHASE_COLORS[phaseStreak.phase] } as CSSProperties}>
            <h2 className="regime-card-heading"><Flame aria-hidden="true" />当前阶段<time dateTime={latest.date}>{latest.date}</time></h2>
            <p className="regime-phase-name regime-tone"
                title={phaseStreak ? `${MARKET_PHASE_GUIDE[phaseStreak.phase].meaning}\n应对: ${MARKET_PHASE_GUIDE[phaseStreak.phase].action}` : undefined}
              >
                <span className="regime-state-dot" style={{ backgroundColor: MARKET_PHASE_COLORS[phaseStreak?.phase ?? 'repair'] }} />
                {MARKET_PHASE_LABELS[phaseStreak?.phase ?? 'repair']}
                {phaseStreak && <small>第 {phaseStreak.streak} 天</small>}
            </p>
            {phaseStreak && (
              <p title={MARKET_PHASE_GUIDE[phaseStreak.phase].meaning}>
                {MARKET_PHASE_GUIDE[phaseStreak.phase].action}
              </p>
            )}
            <div className="regime-mainline-tags" aria-label="当前主线">
              {latestMainlines.length > 0 ? latestMainlines.map(m => (
                <span key={m.member} title={`涨停${m.limit_up_count}家 · 最高${m.max_boards}板 · 梯队${m.rungs_filled}档`}>
                  {m.member}
                </span>
              )) : <span className="text-[11px] text-muted">暂无主线数据</span>}
            </div>
          </section>
          <div className="regime-phase-metrics">
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
              <section key={k.label} className={cn(cardCls, 'regime-phase-metric')} style={{ '--metric-color': k.color } as CSSProperties} aria-label={k.label}>
                <h3>{k.label}</h3>
                <p className="regime-metric-value regime-tone">{k.val ?? '—'}<small>{k.unit}</small></p>
                  {p != null && (
                    <span
                      className="regime-percentile"
                      title={`当前值在所选时间窗口内的历史分位 (p${p})`}
                    >
                      历史分位 P{p}
                    </span>
                  )}
                {k.sub && <p className="regime-metric-note">{k.sub}</p>}
              </section>
            )
          })}
          </div>
        </div>
      ) : (
        <div className="regime-placeholder" role={history.isError ? 'alert' : 'status'}>
          {history.isLoading ? '正在加载情绪周期…' : history.isError ? <><p>情绪周期加载失败，请重试。</p><button type="button" onClick={() => history.refetch()}>重新加载</button></>
            : latest && hasPhaseData ? '最新交易日暂无情绪阶段数据，可点击「重算」补全。'
              : '市场阶段(情绪周期)数据尚未生成 — 点击右上角「重算」即可回填全部历史阶段与主线'}
        </div>
      )}

      {/* ── 阶段规律: 当前阶段的历史统计 + 下阶段转移分布 (窗口内 segments 提炼) ── */}
      {phaseStats && phaseStreak && (
        <div className={cn(cardCls, 'regime-phase-patterns')}>
          <SectionTitle icon={Flame} title="阶段规律"
            hint="当前阶段在窗口内的历史统计 · 去向=每段之后进入的阶段" />
          <div className="regime-phase-stats">
            <span className="text-secondary">
              <span className="font-semibold text-foreground">
                {MARKET_PHASE_LABELS[phaseStreak.phase]}
              </span>
              <span className="text-muted"> 已持续 </span>
              <span className="font-mono text-foreground">{phaseStreak.streak}</span> 天
              <span className="text-muted"> · 历史 {phaseStats.count} 段, 平均 {phaseStats.avgDays} 天, 最长 {phaseStats.maxDays} 天</span>
            </span>
            {phaseStreak.streak > phaseStats.avgDays && (
              <span className="rounded bg-warning/10 px-1.5 py-px text-[12px] font-medium text-secondary"
                title="持续天数已超过窗口内该阶段的平均时长">
                已超历史平均
              </span>
            )}
            {phaseStats.transitions.length > 0 && (
              <span className="flex flex-wrap items-center gap-1.5 text-muted">
                历史去向:
                {phaseStats.transitions.map(t => (
                  <span key={t.phase} className="regime-history-tag"
                    title={`${phaseStats.count} 段中 ${t.count} 段之后进入`}>
                    <span className="regime-state-dot" style={{ backgroundColor: MARKET_PHASE_COLORS[t.phase] }} />
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
        <div className={cardCls}>
          <SectionTitle icon={Flame} title="情绪周期时间轴"
            hint="堆叠柱=涨停(首板+2板+) · 高度(红) · 晋级率(蓝) · ▲切换 · 点击日期回看当日" />
          {/* 当日快照: 点击图表日期出现 — 阶段/梯队指标/当日主线 top3 一屏回看 */}
          {selRow && (
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-accent/25 bg-accent/5 px-2.5 py-1.5">
              <span className="flex items-center gap-1.5">
                <span className="font-mono text-xs font-semibold text-foreground">{selRow.date}</span>
                <span className="regime-history-tag">
                  <span className="regime-state-dot" style={{ backgroundColor: MARKET_PHASE_COLORS[selRow.phase as MarketPhase] }} />
                  {selRow.phase ? MARKET_PHASE_LABELS[selRow.phase] : '—'}
                </span>
                {selStreak != null && <span className="text-[12px] text-muted">第 {selStreak} 天</span>}
              </span>
              <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1 font-mono text-[12px] text-secondary">
                <span>高度 <b className="text-foreground">{selRow.max_consecutive}板</b></span>
                <span>涨停 <b className="text-foreground">{selRow.limit_up ?? '—'}</b>
                  <span className="text-muted"> = 首板{selRow.first_board ?? '—'} + 2板+{selRow.ge2_count ?? '—'}</span></span>
                <span>晋级 <b className="text-foreground">{selRow.promo_rate != null ? (selRow.promo_rate * 100).toFixed(0) + '%' : '—'}</b></span>
                <span>封板 <b className="text-foreground">{selRow.seal_rate != null ? (selRow.seal_rate * 100).toFixed(0) + '%' : '—'}</b></span>
              </span>
              <span className="flex flex-wrap items-center gap-1">
                {selMainlines.length > 0 ? selMainlines.map(m => (
                  <span key={m.member} className="regime-history-tag"
                    title={`涨停${m.limit_up_count}家 · 最高${m.max_boards}板 · 梯队${m.rungs_filled}档`}>
                    {m.member}
                  </span>
                )) : <span className="text-[11px] text-muted">当日无主线数据</span>}
              </span>
              <button onClick={() => setSelDate(null)}
                className="ml-auto flex items-center gap-1 rounded px-1.5 py-px text-[12px] text-muted hover:bg-elevated hover:text-foreground">
                <X className="h-3 w-3" /> 回到今日
              </button>
            </div>
          )}
          <div ref={phaseChartRef} className="regime-chart regime-phase-chart" role="img" aria-label="情绪周期高度、宽度与晋级率历史趋势图" />
          <div className="regime-state-strip" role="img" aria-label="情绪阶段时间轴">
            {rows.map(r => (
              <div key={r.date} title={`${r.date} ${MARKET_PHASE_LABELS[r.phase as MarketPhase]}`}
                className="min-w-0 flex-1"
                style={{ backgroundColor: MARKET_PHASE_COLORS[r.phase as MarketPhase] }} />
            ))}
          </div>
          <div className="regime-legend">
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
        <div className={cardCls}>
          <SectionTitle icon={Layers} title="阶段 × 主线"
            hint={`${segments.length} 段 · 主线按段内 top5 天数排序`} />
          {/* 最大高度内滚动: 段数多时不再拉长页面; 表头吸顶保证滚动时列名可见。
              border-separate 是 sticky 前提 — Chromium 在 border-collapse:collapse
              (preflight 默认) 下表格元素 sticky 失效; spacing-0 保持视觉不变。 */}
          <div className="regime-table-scroll" role="region" aria-label="阶段与主线历史记录" tabIndex={0}>
            <table aria-label="阶段与主线" className="w-full min-w-[840px] border-separate border-spacing-0 text-left text-[12px]">
              <thead>
                <tr className="text-[12px] text-muted">
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
                      <span className="regime-history-tag">
                        <span className="regime-state-dot" style={{ backgroundColor: MARKET_PHASE_COLORS[seg.phase] }} />
                        {seg.label}
                      </span>
                    </td>
                    <td className="border-b border-border/50 py-1.5 pr-3 font-mono text-[12px] text-secondary">
                      {seg.start} ~ {seg.end}
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
                          <span key={m.member} className="regime-history-tag"
                            title={`top5 ${m.top5_days} 天 · 最高 ${m.max_boards} 板 · 龙头 ${m.leader_symbol}`}>
                            {m.member}<span className="ml-1 font-mono opacity-70">{m.top5_days}d</span>
                          </span>
                        )) : <span className="text-[11px] text-muted">—</span>}
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
      {rows.length > 0 && (
      <div className={cardCls}>
        <SectionTitle icon={Layers} title="主线排行"
          hint={
            <span className="flex items-center gap-2">
              <span className="hidden sm:inline text-[11px] text-muted">{mainline.data?.membership_note}</span>
              <button
                onClick={() => setFilterOpen(v => !v)}
                className={cn('inline-flex items-center gap-1 rounded-btn border px-2 py-0.5 text-[12px] transition-colors',
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
          <span className="text-[12px] text-muted">窗口内 top1 天数排序 · 点击「过滤」配置宽基概念屏蔽</span>
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
          <table className="w-full min-w-[560px] text-left text-[12px]">
            <thead>
              <tr className="border-b border-border text-[12px] text-muted">
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
                <tr><td colSpan={5} className="py-4 text-center text-[12px]">
                  {mainline.isLoading ? <span className="text-muted">加载中…</span>
                    : mainline.isError ? (
                      <span className="text-secondary">主线数据加载失败 —{' '}
                        <button type="button" onClick={() => mainline.refetch()} disabled={mainline.isFetching} className="text-accent underline underline-offset-2 transition-colors hover:text-accent/70 disabled:opacity-50">
                          重试
                        </button>
                      </span>
                    )
                    : <span className="text-muted">暂无主线数据 — 点击「重算」回填, 或检查过滤设置</span>}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      )}

      </div>{/* /情绪周期 tab */}

      {/* ══ 市场环境：当前判断 → 驱动因素 → 历史验证 ══ */}
      <div className="regime-view-panel" id="regime-panel-regime" role="tabpanel" aria-labelledby="regime-tab-regime" hidden={view !== 'regime'}>
        {latest ? (
          <div className="regime-snapshot-grid">
            <section className={cn(cardCls, 'regime-snapshot-card')} aria-label="最新环境状态" style={{ '--metric-color': REGIME_STATE_COLORS[latest.state] } as CSSProperties}>
              <h2 className="regime-card-heading"><Gauge aria-hidden="true" />最新状态<time dateTime={latest.date}>{latest.date}</time></h2>
              <div className="regime-status-value">
                <strong className="regime-tone">{REGIME_STATE_LABELS[latest.state]}</strong>
                <span className="regime-score regime-tone" aria-label={`综合分 ${latest.score}，满分 100`}>{latest.score}<small>分</small></span>
              </div>
              <div className="regime-score-meter" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(100, latest.score))}%`, backgroundColor: REGIME_STATE_COLORS[latest.state] }} /></div>
            </section>
            <section className={cn(cardCls, 'regime-snapshot-card')} aria-label="当前环境势头" style={{ '--metric-color': REGIME_STATE_COLORS[latest.state] } as CSSProperties}>
              <h2 className="regime-card-heading">{momentum && momentum.slope > 0 ? <TrendingUp aria-hidden="true" /> : momentum && momentum.slope < 0 ? <TrendingDown aria-hidden="true" /> : <Minus aria-hidden="true" />}当前势头</h2>
              <p className="regime-momentum-value">连续 <strong className="regime-tone">{momentum?.streak ?? '—'}</strong> 天<span className="regime-tone">{REGIME_STATE_LABELS[latest.state]}</span></p>
              <p className="regime-card-note">近 5 日{momentum && momentum.slope > 0 ? '改善' : momentum && momentum.slope < 0 ? '恶化' : '持平'}{momentum && momentum.lastWeakGap > 0 && ` · 上次弱势 ${momentum.lastWeakGap} 天前`}</p>
            </section>
            <section className={cn(cardCls, 'regime-snapshot-card')} aria-label="环境四维拆解">
              <h2 className="regime-card-heading"><Activity aria-hidden="true" />四维拆解</h2>
              <div className="regime-dimensions">
                {([
                  { label: '赚钱', val: latest.profit_score, color: '#f59e0b' },
                  { label: '投机', val: latest.speculation_score, color: '#a855f7' },
                  { label: '抗跌', val: latest.resilience_score, color: '#10b981' },
                  { label: '趋势', val: latest.trend_score, color: '#3b82f6' },
                ] as const).map(d => (
                  <div key={d.label} className="regime-dimension" style={{ '--metric-color': d.color } as CSSProperties}>
                    <span>{d.label}</span><div className="regime-track" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(100, d.val ?? 0))}%`, backgroundColor: d.color }} /></div><strong className="regime-tone">{d.val ?? '—'}</strong>
                  </div>
                ))}
              </div>
            </section>
            <section className={cn(cardCls, 'regime-snapshot-card')} aria-label="环境运行节奏">
              <h2 className="regime-card-heading"><Repeat aria-hidden="true" />状态转换<small>近 {days} 天</small></h2>
              <p className="regime-transition-value">{transitions.count}<small>次切换</small></p>
              <p className="regime-card-note"><span className="text-accent">{transitions.label}</span> · {(transitions.rate * 100).toFixed(0)}%/天</p>
            </section>
          </div>
        ) : history.isLoading ? (
          <div className="regime-loading" role="status" aria-label="正在加载市场环境">
            <div className="regime-loading-line" aria-hidden="true" /><div className="regime-loading-line" aria-hidden="true" /><div className="regime-loading-line" aria-hidden="true" />
          </div>
        ) : (
          <div className="regime-placeholder" role={history.isError ? 'alert' : 'status'}>
            <p>{history.isError ? '环境数据加载失败，请重试。' : '暂无环境数据，请先运行盘后管道或点击「重算」。'}</p>
            {history.isError && <button type="button" onClick={() => history.refetch()} disabled={history.isFetching}>重新加载</button>}
          </div>
        )}

        {rows.length > 0 && (
              <section className={cn(cardCls, 'regime-timeline')}>
                <SectionTitle icon={Activity} title="状态时间轴" hint={`${rows[0].date} — ${latest!.date} · ${rows.length} 天`} />
                <div className="regime-state-strip" role="img" aria-label={`${rows.length} 个交易日的环境状态分布时间轴`}>
                  {rows.map(r => <span key={r.date} title={`${r.date} ${REGIME_STATE_LABELS[r.state]}(${r.score})`} style={{ backgroundColor: REGIME_STATE_COLORS[r.state] }} />)}
                </div>
                <div className="regime-legend">
                  {STATE_ORDER.map(state => <span key={state}><i className="regime-state-dot" style={{ backgroundColor: REGIME_STATE_COLORS[state] }} />{REGIME_STATE_LABELS[state]}</span>)}
                </div>
              </section>
        )}

        {rows.length > 0 && (
          <div className="regime-trend-grid">
            <section className={cardCls} aria-label="市场环境历史趋势">
              <SectionTitle icon={Activity} title="环境综合分趋势" hint="点图例可叠加四维指标与涨停数" />
              <div ref={trendRef} className="regime-chart" role="img" aria-label="环境综合分及四维指标历史趋势图" />
            </section>
            <section className={cardCls} aria-label="历史环境状态分布">
              <SectionTitle icon={Gauge} title="状态分布" />
              {states.isLoading ? <div className="regime-placeholder regime-placeholder-compact" role="status">正在加载状态分布…</div>
                : states.isError ? <div className="regime-placeholder regime-placeholder-compact" role="alert"><p>状态分布加载失败</p><button type="button" onClick={() => states.refetch()} disabled={states.isFetching}>重试</button></div>
                : distributionTotal === 0 ? <div className="regime-placeholder regime-placeholder-compact">所选范围内暂无状态分布</div>
                : <>
                  <div className="regime-distribution-graphic">
                    <div ref={distributionRef} className="regime-distribution-chart" role="img" aria-label="历史环境状态占比环形图" />
                    <p className="regime-distribution-center" aria-hidden="true"><strong>{distributionTotal}</strong><span>交易日</span></p>
                  </div>
                  <dl className="regime-distribution-list">
                    {distribution.map(item => {
                      const percent = item.count / distributionTotal * 100
                      return <div key={item.state}>
                        <div className="regime-distribution-label"><span className="regime-state-dot" style={{ backgroundColor: REGIME_STATE_COLORS[item.state] }} /><dt>{item.label}</dt><dd className="regime-distribution-count">{item.count} 天<small>{percent.toFixed(1)}%</small></dd></div>

                      </div>
                    })}
                  </dl>
                </>}
            </section>
          </div>
        )}

        {calendarMonths.length > 0 && (
          <section className={cardCls}>
            <SectionTitle icon={CalendarDays} title="日历热力图" hint={
              <button type="button" onClick={() => setCalendarExpanded(v => !v)} aria-expanded={calendarExpanded} aria-controls="regime-calendar"
                className="inline-flex min-h-8 items-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary hover:bg-elevated">
                {calendarExpanded ? <><Rows3 className="h-3.5 w-3.5" />收起月份</> : <><LayoutGrid className="h-3.5 w-3.5" />展开全部月份</>}
              </button>
            } />
            <div ref={calendarScrollRef} id="regime-calendar" role="region" aria-label="市场环境日历" tabIndex={0}
              className={cn('regime-calendar', calendarExpanded && 'regime-calendar-expanded')}>
              {calendarMonths.map(month => {
                const monthRows = month.weeks.flat().filter((cell): cell is RegimeRow => !!cell)
                const average = monthRows.length > 0 ? Math.round(monthRows.reduce((sum, row) => sum + row.score, 0) / monthRows.length) : null
                return <div key={`${month.year}-${month.month}`} className="regime-calendar-month">
                  <div className="regime-calendar-month-title"><span>{month.label}</span><strong title="月均综合分">{average ?? '—'}</strong></div>
                  <div className="regime-calendar-grid regime-calendar-weekdays" aria-hidden="true">
                    {['一', '二', '三', '四', '五', '六', '日'].map(day => <span key={day}>{day}</span>)}
                  </div>
                  <div className="regime-calendar-grid">
                    {month.weeks.flat().map((cell, index) => cell ?
                      <div key={index} className="regime-calendar-day" title={`${cell.date} ${REGIME_STATE_LABELS[cell.state]}(${cell.score})`} style={{ backgroundColor: REGIME_STATE_COLORS[cell.state] }} />
                      : <div key={index} className="regime-calendar-day" />)}
                  </div>
                </div>
              })}
            </div>
            <p className="mt-3 text-xs text-secondary">颜色表示每日环境状态，月份右侧为月均综合分。</p>
          </section>
        )}
      </div>

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

  // 面板可能先于过滤配置查询挂载: 首次拿到真实 filter 时回填一次, 避免用户在
  // 默认值上点「保存并重算」用 4/600 覆盖真实过滤配置; 只同步首轮, 不打断编辑
  const seededRef = useRef(false)
  useEffect(() => {
    if (!filter || seededRef.current) return
    seededRef.current = true
    setMinMembers(String(filter.min_members))
    setMaxMembers(String(filter.max_members))
    setBlacklist(filter.blacklist)
    setExcludeSt(filter.exclude_st ?? true)
  }, [filter])

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
          <span className="text-[12px] text-muted">成员数上限(过滤宽基标签)</span>
          <input type="number" min={50} max={5000} value={maxMembers}
            onChange={e => setMaxMembers(e.target.value)}
            className="h-7 w-24 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[12px] text-muted">成员数下限</span>
          <input type="number" min={1} max={200} value={minMembers}
            onChange={e => setMinMembers(e.target.value)}
            className="h-7 w-20 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
        </label>
        <div className="flex min-w-0 flex-1 basis-[220px] flex-col gap-1">
          <span className="text-[12px] text-muted">按名称屏蔽(回车添加)</span>
          <div className="flex items-center gap-1.5">
            <input value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTag() } }}
              placeholder="如: 融资融券、沪股通"
              className="h-7 min-w-0 flex-1 rounded-input border border-border bg-base px-2 text-xs text-foreground outline-none focus:border-accent" />
          </div>
          {blacklist.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {blacklist.map(b => (
                <span key={b} className="inline-flex items-center gap-1 rounded bg-accent/10 px-1.5 py-px text-[12px] text-accent">
                  {b}
                  <button type="button" aria-label={`取消屏蔽 ${b}`} onClick={() => setBlacklist(blacklist.filter(x => x !== b))} className="hover:text-foreground">
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
        <span className="text-[12px] text-secondary">
          统计剔除 ST 股
          <span className="ml-1.5 text-[11px] text-muted">主线 + 情绪周期统一口径; 切换后自动全量重算(约 1-2 分钟)</span>
        </span>
      </div>
      <div className="mt-1.5 text-[11px] text-muted">
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
          <div className="mt-0.5 text-[12px] text-muted">范围 1 ~ 1000 个交易日</div>
        </div>
        <input
          ref={inputRef}
          type="number"
          aria-label="自定义交易日天数"
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
              className="h-6 rounded-btn border border-border bg-base px-2 text-[12px] text-secondary hover:text-accent hover:border-accent/40 transition-colors">
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
