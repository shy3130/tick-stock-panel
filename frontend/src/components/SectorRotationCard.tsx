/**
 * 板块切换卡片 (盘中轮动) — 概念/行业分析页共用。
 *
 * 数据: GET /api/sector-rotation (全量分钟聚合, 概念/行业二选一由页面 kind 决定),
 * 30s 前端轮询实时刷新 (分钟数据后端 6s 增量落盘, 30s 粒度已足够盘中观察)。
 * 展示板块来源: 自动榜 (维度可选: 活跃度=近 30 分钟成交额合计 / 综合分 / 现涨幅 /
 * 切入=1h 排名跃升 / 资金流; 行数 5/10/15/20; 自动剔除属性板块黑名单与超成员数
 * 上限的大桶, 名单可编辑, localStorage 按 kind 持久化) 或自定义监控清单
 * (≤20, localStorage 按 kind 持久化); 显示模式: 走势线 (默认) / 热力图。
 * 资金流维度来自用户选择的扩展数据列; 指数叠加线来自 /api/index/* (核心四只)。
 * 布局 (issue #518): 宽屏展示图 (aspect 9:5) 在左, 右列=切换强度线+板块榜单
 * (右列绝对定位脱离行高计算, 榜单列内滚动, 两列底部齐平); 窄屏上下堆叠。
 * 时间轴全天铺刻度 (09:30–15:00), 盘中数据止于当前桶、右侧留白。
 * 强度图与展示图 x 轴窗口一致, 经 echarts.connect 联动指针; 榜单行与展示图双向高亮;
 * 板块配色按名称稳定分配 (colorForSector), 走势线/易主圆点/切换图标注/榜单圆点同源。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Activity, Database, RefreshCw, SlidersHorizontal } from 'lucide-react'
import { api, type SectorRotationSector } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { cn } from '@/lib/cn'
import { toast } from '@/components/Toast'
import { CORE_INDEXES } from '@/components/Layout'

const FLOW_LS_PREFIX = 'sector_rotation_flow_'
const INDEX_LS_PREFIX = 'sector_rotation_index_'
const ROWS_MODE_PREFIX = 'sector_rotation_rows_'
const CUSTOM_NAMES_PREFIX = 'sector_rotation_custom_'
const DISPLAY_MODE_PREFIX = 'sector_rotation_display_'
const AUTO_ROWS_PREFIX = 'sector_rotation_auto_rows_'
const EXCLUDE_PREFIX = 'sector_rotation_exclude_'
// 自动活跃榜展示行数可选项; excludeNames === null 表示未自定义 (用后端内置名单)
const AUTO_ROW_OPTIONS = [5, 10, 15, 20]
// 走势线末端常显名称上限: ≤此数全显; 15/20 行高密模式按最新值封顶 Top N。
// 与 AUTO_ROW_OPTIONS 的默认「前10」对齐 — 常规视图所见即所名; 更多标签会在
// 汇聚区被防重叠推离各自线端 (endLabel 无引导线), 高密模式牺牲常显名换可读性
const END_LABEL_MAX = 10
const MAX_EXCLUDE_SECTORS = 100
// 板块来源: 自动榜排序维度 + 自定义监控
type SectorSource = 'activity' | 'score' | 'pct' | 'rank_change' | 'momentum' | 'flow' | 'custom'
const SOURCE_LABELS: Record<Exclude<SectorSource, 'custom'>, string> = {
  activity: '活跃', score: '综合分', pct: '现涨幅', rank_change: '切入', momentum: '强弱切换', flow: '资金流',
}
function parseSectorSource(raw: string | null): SectorSource {
  if (raw === 'custom' || raw === 'score' || raw === 'pct' || raw === 'rank_change' || raw === 'momentum' || raw === 'flow') return raw
  return 'momentum' // 未设置过 / 旧默认值 → 默认强弱切换 (显式选择过的值已持久化, 不受影响)
}
// 扩展表 schema 的类型名可能是配置里的简写 (float/int), 也可能是 Parquet 的
// 真实类型 (DOUBLE/BIGINT/DECIMAL(18,4))。两种口径都要认, 否则会漏掉整数列。
const NUMERIC_TYPE_PATTERN =
  /^(float|int|number|double|long|real|decimal|numeric|hugeint|ubigint|bigint|u?smallint|u?tinyint|u?integer)/
const isNumericColumnType = (type: unknown) => NUMERIC_TYPE_PATTERN.test(String(type ?? '').toLowerCase())
// 强度图与展示图跨实例联动分组 (x 轴指针同步)
const CHART_CONNECT_GROUP = 'sector-rotation-card'

// 热力图: 行 = 展示板块 (自动模式取前 12 行, 自定义模式最多 20 行);
// 1 分钟桶全天 240+ 列不可读, 只看最近 60 列
const HEAT_ROWS = 12
const HEAT_COLS_1M = 60
const HEAT_ROW_HEIGHT = 26
// 走势线行数上限 = 自定义监控上限
const MAX_CUSTOM_SECTORS = 20
// 走势线调色板 (≤20 条线各自可辨)。原版有两组近绿、三组琥珀、一组蓝青,
// 1.4px 细线 + 抗锯齿下极易认错线 (issue #518); 现按色相族间距挑选,
// 后段用同族加深变浅扩容 — 相邻摆放时仍可靠明度区分。
const TREND_COLORS = [
  '#60a5fa', '#f87171', '#34d399', '#fbbf24', '#a78bfa', '#f472b6', '#fb923c', '#22d3ee',
  '#818cf8', '#a3e635', '#e879f9', '#2dd4bf', '#f43f5e', '#7c3aed', '#d946ef', '#14b8a6',
  '#94a3b8', '#fca5a5', '#67e8f9', '#c7d2fe',
]
// 未进展示矩阵的榜单板块: 灰色占位点 (此前 transparent 看着像漏画了一半)
const SECTOR_DOT_UNMATCHED = 'rgba(128,140,160,0.4)'

// 板块稳定配色: 按名称首次出现分配并缓存 — 下标取色会随排序维度/名单刷新漂移,
// 刚记住的「颜色↔板块」映射就失效了 (issue #518)。走势线/易主圆点/切换图
// 标注/榜单圆点统一走这里, 上下两张图里同一板块同色。
const sectorColorCache = new Map<string, string>()
export function colorForSector(name: string): string {
  let color = sectorColorCache.get(name)
  if (color === undefined) {
    color = TREND_COLORS[sectorColorCache.size % TREND_COLORS.length]
    sectorColorCache.set(name, color)
  }
  return color
}

/**
 * 全天时间轴刻度 (issue #518): 盘中类目轴只铺已产生的桶, 32 个点被均匀铺满
 * 整个绘图区, 线顶到右缘像走完了全天; 分时图习惯是画到当前时刻、右侧留白。
 * 刻度必须与后端桶标签逐格对齐: 后端按桶起点截断 (dt.truncate), 上午最后一桶
 * 是 11:25 (不是 11:30), 下午含 15:00 收盘桶 — 按此生成, 5 分钟桶全天 49 点。
 */
export function fullDayBuckets(bucketMinutes: number): string[] {
  const labels: string[] = []
  const push = (minutes: number) => {
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    labels.push(`${h < 10 ? '0' : ''}${h}:${m < 10 ? '0' : ''}${m}`)
  }
  for (let t = 570; t < 690; t += bucketMinutes) push(t)   // 09:30 起, 上午最后一桶起点 < 11:30
  for (let t = 780; t <= 900; t += bucketMinutes) push(t)  // 13:00 起到 15:00 (含收盘桶)
  return labels
}

/** 全天刻度 ∪ 窗口实际桶标签 (数据侧边界约定变化时并集兜底, 不丢列); HH:MM 字典序即时间序。 */
export function mergeFullDayBuckets(windowBuckets: string[], bucketMinutes: number): string[] {
  const axis = fullDayBuckets(bucketMinutes)
  const have = new Set(axis)
  const extra = windowBuckets.filter(label => !have.has(label))
  return extra.length ? [...axis, ...extra].sort() : axis
}

/** 一次「领涨易主」: col 列起 rowIdx 板块接管 (prevRowIdx=前任, 窗口开局时为 null) */
export interface LeaderReign {
  col: number
  rowIdx: number
  prevRowIdx: number | null
}

/**
 * 逐列找展示板块中桶涨幅最高的行 (null 跳过, 整列无数据则跳过),
 * 相邻有效列的 top 行变化即「领涨易主」。col 为矩阵绝对列号。
 */
export function computeLeaderReigns(matrix: (number | null)[][], startCol: number): LeaderReign[] {
  const reigns: LeaderReign[] = []
  const totalCols = matrix[0]?.length ?? 0
  let prevTop: number | null = null
  for (let col = Math.max(0, startCol); col < totalCols; col++) {
    let top: number | null = null
    let topVal = -Infinity
    for (let row = 0; row < matrix.length; row++) {
      const v = matrix[row]?.[col]
      if (v != null && v > topVal) { topVal = v; top = row }
    }
    if (top == null) continue
    if (top !== prevTop) {
      reigns.push({ col, rowIdx: top, prevRowIdx: prevTop })
      prevTop = top
    }
  }
  return reigns
}

/** reigns → 每列的 top 行 (null=该列无数据), 供热力图领涨轨迹描边 */
export function topRowPerCol(reigns: LeaderReign[], totalCols: number): (number | null)[] {
  const tops: (number | null)[] = new Array(totalCols).fill(null)
  for (let k = 0; k < reigns.length; k++) {
    const end = k + 1 < reigns.length ? reigns[k + 1].col : totalCols
    for (let col = reigns[k].col; col < end; col++) tops[col] = reigns[k].rowIdx
  }
  return tops
}

/**
 * 易主标注截断: 频繁易主时全标会糊成一片 — 保留开局领涨 + 最新一次,
 * 其余按「在位时长」取最长者, 共 ≤max 个。
 */
export function selectReignLabels(reigns: LeaderReign[], totalCols: number, max = 8): Set<number> {
  if (reigns.length <= max) return new Set(reigns.map(r => r.col))
  const first = reigns[0].col
  const last = reigns[reigns.length - 1].col
  const rest = reigns
    .map((r, i) => ({ col: r.col, len: (i + 1 < reigns.length ? reigns[i + 1].col : totalCols) - r.col }))
    .filter(r => r.col !== first && r.col !== last)
    .sort((a, b) => b.len - a.len)
    .slice(0, Math.max(0, max - 2))
    .map(r => r.col)
  return new Set([first, ...rest, last])
}

interface HeatEventParams {
  seriesType?: string
  seriesName?: string
  /** 类目名 (bar/类目轴事件携带, 如板块名) */
  name?: string
  /** ECharts 事件原始 value (按 series 类型不同结构不同), 使用侧自行收窄 */
  value?: unknown
}

function beijingDateParts(): { date: string; minutes: number } {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(new Date())
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? '00'
  return {
    date: `${get('year')}-${get('month')}-${get('day')}`,
    minutes: Number(get('hour')) * 60 + Number(get('minute')),
  }
}

/** 此刻是否处于 A 股连续竞价时段 (9:30-11:30 / 13:00-15:00, 北京时间)。 */
function isMarketSessionNow(): boolean {
  const { minutes } = beijingDateParts()
  return (minutes >= 570 && minutes < 690) || (minutes >= 780 && minutes < 900)
}

function useEChart(
  option: echarts.EChartsOption | null,
  events?: {
    onMouseOver?: (params: HeatEventParams) => void
    onMouseOut?: (params: HeatEventParams) => void
    onGlobalOut?: () => void
  },
  // 容器挂载状态的变化键 (如 displayMode): 容器随条件渲染卸载/重挂但 option 引用
  // 不变时, 仅靠 [option] 依赖 effect 不会重跑, 新 div 永远不 init → 图表空白
  reviveKey?: string | number,
) {
  const ref = useRef<HTMLDivElement>(null)
  const instRef = useRef<echarts.ECharts | null>(null)
  const eventsRef = useRef(events)
  eventsRef.current = events
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
    if (!ref.current) {
      // 容器随 loading/模式分支被卸载: 旧实例绑在已脱离 DOM 的节点上, 直接释放
      if (instRef.current && !instRef.current.isDisposed()) instRef.current.dispose()
      instRef.current = null
      return
    }
    // 容器可能被卸载重挂 (loading 分支) 或被 React 原地复用换挂 ref (模式切换),
    // 旧实例要么已脱离 DOM 要么已被外部 dispose — 三种状态分别处理:
    if (instRef.current) {
      if (instRef.current.isDisposed()) {
        instRef.current = null
      } else if (instRef.current.getDom() !== ref.current) {
        instRef.current.dispose()
        instRef.current = null
      }
    }
    // dom 上残留的外来实例 (HMR/StrictMode 竞态) 必须先清掉, 否则 init 会
    // 警告 "already initialized" 并复用僵尸实例, 后续 setOption 全部落空
    const leftover = echarts.getInstanceByDom(ref.current)
    if (leftover && leftover !== instRef.current) leftover.dispose()
    if (!instRef.current) instRef.current = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    const inst = instRef.current
    // 事件回调经 ref 取最新闭包, 绑定只需覆盖实例生命周期
    inst.off('mouseover'); inst.off('mouseout'); inst.off('globalout')
    inst.on('mouseover', (p: HeatEventParams) => eventsRef.current?.onMouseOver?.(p))
    inst.on('mouseout', (p: HeatEventParams) => eventsRef.current?.onMouseOut?.(p))
    inst.on('globalout', () => eventsRef.current?.onGlobalOut?.())
    if (option) {
      inst.setOption(option, { notMerge: true })
      inst.resize()
    }
  }, [option, reviveKey])
  return { ref, instRef }
}

function fmtPct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function fmtFlow(value: number | null | undefined): string {
  if (value == null) return '—'
  const abs = Math.abs(value)
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)}万`
  return value.toFixed(0)
}

function pctClass(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-muted'
  return value > 0 ? 'text-bull' : 'text-bear'
}

/** 图例条: 用与图上一致的色块/描边/线型直接表达语义, 替代大段括号说明文字 */
function Legend({ items }: { items: { swatch?: ReactNode; label: string }[] }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-1 pb-1 text-[10px] text-muted">
      {items.map(item => (
        <span key={item.label} className="inline-flex items-center gap-1">
          {item.swatch}
          {item.label}
        </span>
      ))}
    </div>
  )
}
function swatchBlock(background: string) {
  return <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-[2px]" style={{ background }} />
}
function swatchFrame(borderColor: string) {
  return <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-[2px] border bg-base/40" style={{ borderColor }} />
}
function swatchLine(background: string) {
  return <span aria-hidden className="inline-block h-[2px] w-3.5 rounded-full" style={{ background }} />
}
function swatchDot(background: string) {
  return <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full" style={{ background }} />
}
function swatchDash(borderColor: string) {
  return <span aria-hidden className="inline-block w-3.5 border-t border-dashed" style={{ borderColor }} />
}

const NO_DATA_HINTS: Record<string, string> = {
  minute_missing: '尚无全市场分钟数据 — 需开启全量分钟落盘 (数据源设置 → 全量分钟路由)',
  minute_schema: '分钟数据分区读取失败, 请检查数据目录',
  minute_empty: '当日分钟数据为空',
  members_missing: '板块成分数据缺失 — 请先在数据页获取概念/行业分类扩展数据',
  no_member_bars: '当日分钟数据中没有板块成分股的行情',
}

export function SectorRotationCard({ kind }: { kind: 'concept' | 'industry' }) {
  const dimLabel = kind === 'industry' ? '行业' : '概念'
  const [flow, setFlow] = useState<string>(() => localStorage.getItem(`${FLOW_LS_PREFIX}${kind}`) ?? '')
  const [bucket, setBucket] = useState(5)
  const [hoverName, setHoverName] = useState<string | null>(null)
  // 展示板块来源: 自动榜 (5 种排序维度, 剔除属性板块) 或自定义监控 (≤20)
  const [rowsMode, setRowsMode] = useState<SectorSource>(() =>
    parseSectorSource(localStorage.getItem(`${ROWS_MODE_PREFIX}${kind}`)))
  const [customNames, setCustomNames] = useState<string[]>(() => {
    try {
      const parsed = JSON.parse(localStorage.getItem(`${CUSTOM_NAMES_PREFIX}${kind}`) ?? '[]')
      return Array.isArray(parsed) ? parsed.filter((n: unknown) => typeof n === 'string').slice(0, MAX_CUSTOM_SECTORS) : []
    } catch {
      return []
    }
  })
  // 自动活跃榜行数 (5/10/15/20) 与排除名单; excludeNames === null = 未自定义 (后端内置名单)
  const [autoRows, setAutoRows] = useState<number>(() => {
    const parsed = Number(localStorage.getItem(`${AUTO_ROWS_PREFIX}${kind}`))
    return AUTO_ROW_OPTIONS.includes(parsed) ? parsed : 10
  })
  const [excludeNames, setExcludeNames] = useState<string[] | null>(() => {
    try {
      const raw = localStorage.getItem(`${EXCLUDE_PREFIX}${kind}`)
      if (raw === null) return null
      const parsed = JSON.parse(raw)
      return Array.isArray(parsed) ? parsed.filter((n: unknown) => typeof n === 'string').slice(0, MAX_EXCLUDE_SECTORS) : null
    } catch {
      return null
    }
  })
  const [excludeEditorOpen, setExcludeEditorOpen] = useState(false)
  const [excludeInput, setExcludeInput] = useState('')
  // 显示模式: trend = 每板块一条涨幅走势线 (默认), heatmap = 热力图
  const [displayMode, setDisplayMode] = useState<'trend' | 'heatmap'>(() =>
    localStorage.getItem(`${DISPLAY_MODE_PREFIX}${kind}`) === 'heatmap' ? 'heatmap' : 'trend')
  // 次级图表选项 (指数叠加/资金流/分钟粒度) 折叠开关 — 主行只留高频控件
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerSearch, setPickerSearch] = useState('')

  const schemaQuery = useQuery({
    queryKey: QK.extSchemaAll,
    queryFn: api.extDataSchemaAll,
    staleTime: 300_000,
  })
  // 资金流候选: 全部扩展表的数值列 (id.列名), 不写死任何表
  const flowOptions = useMemo(() => {
    const options: { value: string; label: string }[] = []
    for (const item of schemaQuery.data?.items ?? []) {
      for (const column of item.columns ?? []) {
        if (!isNumericColumnType(column.type)) continue
        options.push({
          value: `${item.id}.${column.name}`,
          label: `${item.label || item.id} · ${column.label || column.name}`,
        })
      }
    }
    return options
  }, [schemaQuery.data])

  // 自定义模式把监控清单传给后端 (空清单 = 按活跃榜展示);
  // 自动模式传排序维度、行数与排除名单 ([] = 清空名称过滤, null = 用后端内置名单)
  const seriesNames = rowsMode === 'custom' ? customNames : []
  const seriesKey = seriesNames.join(',')
  const filterKey = rowsMode === 'custom'
    ? ''
    : `${rowsMode}|${autoRows}|${excludeNames === null ? 'default' : JSON.stringify(excludeNames)}`
  const rotationQuery = useQuery({
    queryKey: QK.sectorRotation(kind, flow, bucket, seriesKey, filterKey),
    queryFn: () => api.sectorRotation({
      kind, flow: flow || undefined, bucket,
      seriesNames: seriesNames.length ? seriesNames : undefined,
      autoRows: rowsMode === 'custom' ? undefined : autoRows,
      excludeSectors: rowsMode === 'custom' || excludeNames === null ? undefined : excludeNames,
      sortBy: rowsMode === 'custom' ? undefined : rowsMode,
    }),
    // 非连续竞价时段 (回放日期/盘前/盘后) 轮动矩阵不变, 降为 5 分钟兜底;
    // 开盘后间隔函数随下次调度自动回到 30s (phase 计算与 isMarketSessionNow 同源)
    refetchInterval: () => (isMarketSessionNow() ? 30_000 : 300_000),
    staleTime: 25_000,
  })
  const data = rotationQuery.data
  // 编辑器展示名单: 未自定义时用后端内置名单预填 (响应缺省时为空)
  const effectiveExclude = excludeNames ?? (data?.default_exclude_sectors ?? [])
  const displayNames = data?.series?.sectors ?? []
  const maxHeatRows = rowsMode === 'custom' ? MAX_CUSTOM_SECTORS : Math.max(HEAT_ROWS, autoRows)
  const heatRows = Math.min(maxHeatRows, displayNames.length)
  const heatNames = displayNames.slice(0, heatRows)

  // 盘中/回放: 轮动日期 == 北京今天且处于连续竞价时段 → 盘中 (仅在连续竞价时
  // 最后一桶才是"未封口"的; 午休/盘前/收盘后一律按回放呈现)
  const phase: 'live' | 'replay' = useMemo(() => {
    if (!data || data.status !== 'ok' || !data.date) return 'replay'
    const { date: today } = beijingDateParts()
    if (data.date !== today) return 'replay'
    return isMarketSessionNow() ? 'live' : 'replay'
  }, [data])

  // 指数叠加线 (核心四只, 默认上证): 分钟桶涨幅对齐轮动时间轴, 昨收基准
  const [indexSymbol, setIndexSymbol] = useState<string>(
    () => localStorage.getItem(`${INDEX_LS_PREFIX}${kind}`) ?? CORE_INDEXES[0].symbol,
  )
  const indexLabel = CORE_INDEXES.find(item => item.symbol === indexSymbol)?.name ?? indexSymbol
  const indexMinuteQuery = useQuery({
    queryKey: QK.sectorRotationIndexMinute(indexSymbol, data?.date),
    queryFn: () => api.indexMinute(indexSymbol, data!.date!),
    // 指数分钟是实时接口 (后端仅当日有效): 回放日期不请求, 避免对过去日期的徒劳网络等待
    enabled: !!data?.date && phase === 'live',
    refetchInterval: 30_000,
    staleTime: 25_000,
  })
  const indexDailyQuery = useQuery({
    queryKey: QK.sectorRotationIndexDaily(indexSymbol),
    queryFn: () => api.indexDaily(indexSymbol, 20),
    enabled: !!data?.date,
    staleTime: 300_000,
  })
  const onIndexChange = (value: string) => {
    setIndexSymbol(value)
    localStorage.setItem(`${INDEX_LS_PREFIX}${kind}`, value)
  }

  // 指数逐桶涨幅: 分钟 bar 涨幅 (昨收基准, 缺昨收退化首根收盘) 按桶均值,
  // 时间轴对齐 timeline 的 HH:MM — 与全市场线同口径 (累计涨幅), 供切换强度图叠加
  const indexLine = useMemo(() => {
    if (!data || data.status !== 'ok' || !data.date) return null
    const rotationDate = data.date
    const rows = indexMinuteQuery.data?.rows ?? []
    if (!rows.length) return null
    const daily = (indexDailyQuery.data?.rows ?? []).filter(row => row.date < rotationDate)
    const prevClose = daily.length ? Number(daily[daily.length - 1].close) : null
    const refPx = prevClose ?? Number(rows[0].close)
    const byTime = new Map<string, { sum: number; n: number }>()
    for (const row of rows) {
      const time = row.datetime.slice(11, 16)
      if (!time) continue
      const pct = row.close / refPx - 1
      const cell = byTime.get(time)
      if (cell) { cell.sum += pct; cell.n += 1 } else byTime.set(time, { sum: pct, n: 1 })
    }
    if (!byTime.size) return null
    return {
      label: indexLabel,
      points: data.timeline.map(point => {
        const cell = byTime.get(point.time)
        return cell ? cell.sum / cell.n : null
      }),
    }
  }, [data, indexLabel, indexMinuteQuery.data, indexDailyQuery.data])

  // 两图共享同一 x 轴窗口 (1 分钟桶只看最近 60 列), 经 echarts.connect 联动指针
  const timeline = data?.timeline ?? []
  const total = timeline.length
  const startCol = bucket === 1 ? Math.max(0, total - HEAT_COLS_1M) : 0
  const cols = Math.max(0, total - startCol)

  const chartTheme = useChartTheme()

  // 展示图 (trend): 每个展示板块一条累计涨幅走势线, y = 桶内涨幅均值 (右轴 %)
  const trendOption = useMemo<echarts.EChartsOption | null>(() => {
    const heatSeries = data?.series
    if (!heatSeries || !heatSeries.sectors.length || !heatSeries.buckets.length) return null
    const winBuckets = heatSeries.buckets.slice(startCol)
    // 全天铺轴 (issue #518): 仅全窗口视图铺 — 1 分钟桶的「最近 60 列」是滑动
    // 窗口不是全天进度视图, 不铺。盘中未产生的桶为 null → 线止于当前时刻,
    // 右侧留白到 15:00, 扫一眼即知当天进行到哪。
    const axisBuckets = startCol === 0 ? mergeFullDayBuckets(winBuckets, bucket) : winBuckets
    const dataIndexOf = new Map(winBuckets.map((label, i) => [label, i] as const))
    // 领涨易主: 逐列 top 行变化 → 在接管板块的线上标圆点+板块名 (频繁易主时按在位时长截断)
    const totalCols = heatSeries.buckets.length
    const reigns = computeLeaderReigns(heatSeries.matrix, startCol)
    const labelCols = selectReignLabels(reigns, totalCols)
    // 线末端常显板块名: ≤END_LABEL_MAX 条全显 (默认「前10」下所见即所名);
    // 15/20 行高密模式按最新值封顶 Top END_LABEL_MAX (其余靠悬停聚焦/图例)
    const latestVals = heatSeries.sectors.map((_, i) => {
      const row = heatSeries.matrix[i] ?? []
      for (let c = row.length - 1; c >= 0; c--) { const v = row[c]; if (v != null) return v }
      return -Infinity
    })
    const endLabelRows = new Set(
      heatSeries.sectors.length <= END_LABEL_MAX
        ? heatSeries.sectors.map((_, i) => i)
        : heatSeries.sectors.map((_, i) => i).sort((a, b) => latestVals[b] - latestVals[a]).slice(0, END_LABEL_MAX),
    )
    return {
      // bottom 预留两行 plain 图例的高度 (scroll 图例会翻页, 一眼看不全所有板块)
      grid: { left: 44, right: 78, top: 12, bottom: 56 },
      legend: {
        type: 'plain', bottom: 0, left: 'center', width: '94%',
        itemWidth: 14, itemHeight: 2, itemGap: 8,
        textStyle: { fontSize: 10, color: chartTheme.text },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: chartTheme.tooltipBg,
        borderColor: chartTheme.tooltipBorder,
        textStyle: { color: chartTheme.tooltipText, fontSize: 10 },
        formatter: (params: unknown) => {
          const list = (Array.isArray(params) ? params : [params]) as Array<{
            axisValue?: string; seriesName?: string; marker?: string; value?: number | null
          }>
          const axisValue = list[0]?.axisValue ?? ''
          const idx = dataIndexOf.get(axisValue)
          const point = idx === undefined ? undefined : timeline[startCol + idx]
          // 全天铺轴后右侧留白区无数据点: 至少回显时刻
          if (!point) return `<b>${axisValue}</b>`
          const lines = [
            `<b>${point.time}</b>`,
            `领涨${dimLabel} ${point.leader} (${fmtPct(point.leader_pct)})`,
            `全市场 ${fmtPct(point.market_pct)}`,
          ]
          const withValues = list
            .filter(item => item.seriesName && typeof item.value === 'number')
            .map(item => ({ name: item.seriesName!, value: item.value as number, marker: item.marker ?? '' }))
            .sort((a, b) => b.value - a.value)
          for (const item of withValues) lines.push(`${item.marker} ${item.name} ${fmtPct(item.value)}`)
          if (phase === 'live' && idx === cols - 1) lines.push('<span style="color:#f59e0b">进行中的桶 · 数据未封口</span>')
          return lines.join('<br/>')
        },
      },
      xAxis: {
        type: 'category', data: axisBuckets,
        axisLine: { show: false }, axisTick: { show: false },
        // 目标 6 个刻度; hideOverlap 兜底 — ECharts 对最大刻度有"强制显示"倾向,
        // 取整后的间隔会让末尾刻度与它挤在一起
        axisLabel: {
          fontSize: 9, color: chartTheme.text, hideOverlap: true,
          interval: Math.max(0, Math.ceil(axisBuckets.length / 6) - 1),
          showMaxLabel: true,
        },
      },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 9, color: chartTheme.text, formatter: (v: number) => `${(v * 100).toFixed(1)}%` },
        splitLine: { lineStyle: { color: 'rgba(128,140,160,0.15)' } },
      },
      series: heatSeries.sectors.map((name, i) => {
        const color = colorForSector(name)
        const row = heatSeries.matrix[i] ?? []
        // 数据对齐全天轴: 窗口内的桶取真实值, 未产生/缺失的桶为 null (右侧留白)
        const linedata = axisBuckets.map(label => {
          const winIdx = dataIndexOf.get(label)
          return winIdx === undefined ? null : row[startCol + winIdx] ?? null
        })
        // 本线的易主标注点 (接管时点), 坐标 = 桶标签 + 当列涨幅
        const switchPoints = reigns
          .filter(r => r.rowIdx === i && labelCols.has(r.col))
          .map(r => ({
            name,
            coord: [heatSeries.buckets[r.col], heatSeries.matrix[i]?.[r.col] ?? 0] as [string, number],
          }))
        return {
          name,
          type: 'line' as const,
          smooth: true,
          symbol: 'none',
          data: linedata,
          lineStyle: { color, width: 1.4 },
          connectNulls: true,
          // 悬停聚焦 (图上线条或榜单行均可触发): 目标线加粗并在末端浮出板块名,
          // 其余线压到近透明 — 多线并行时不用再靠颜色逐一匹配
          emphasis: {
            focus: 'series',
            lineStyle: { width: 2.6 },
            label: {
              show: true, formatter: () => name, position: 'top' as const,
              fontSize: 9, color,
              textBorderColor: chartTheme.tooltipBg, textBorderWidth: 2,
            },
          },
          blur: { lineStyle: { opacity: 0.06 } },
          // 线末端常显板块名, shiftY 防末端标签叠罗汉
          ...(endLabelRows.has(i) ? {
            endLabel: {
              show: true,
              formatter: name,
              fontSize: 9.5,
              color,
              textBorderColor: chartTheme.tooltipBg,
              textBorderWidth: 2,
              distance: 4,
            },
            labelLayout: { moveOverlap: 'shiftY' as const, hideOverlap: false },
          } : {}),
          // 领涨易主时点: 圆点 + 板块名 (silent 不抢悬停聚焦)
          ...(switchPoints.length ? {
            markPoint: {
              symbol: 'circle',
              symbolSize: 5,
              silent: true,
              itemStyle: { color },
              label: {
                show: true,
                formatter: name,
                position: 'top' as const,
                distance: 5,
                fontSize: 9.5,
                color,
                textBorderColor: chartTheme.tooltipBg,
                textBorderWidth: 2,
              },
              data: switchPoints,
            },
          } : {}),
          // 首条线挂 0% 参考虚线, 便于分辨板块在线上/线下
          ...(i === 0 ? {
            markLine: {
              silent: true, symbol: 'none',
              lineStyle: { color: chartTheme.border, type: 'dashed' as const, width: 1 },
              data: [{ yAxis: 0 }], label: { show: false },
            },
          } : {}),
        }
      }),
    }
  }, [data, bucket, startCol, timeline, chartTheme, phase, dimLabel, cols])
  const trend = useEChart(trendOption, {
    onMouseOver: (params) => {
      if (params.seriesType === 'line' && params.seriesName) setHoverName(params.seriesName)
    },
    onMouseOut: () => setHoverName(null),
    onGlobalOut: () => setHoverName(null),
  }, displayMode)

  // 展示图 (heatmap): 行 = 展示板块, 色 = 该桶板块涨幅 (红涨绿跌)
  const heatOption = useMemo<echarts.EChartsOption | null>(() => {
    const heatSeries = data?.series
    if (!heatSeries || !heatSeries.sectors.length || !heatSeries.buckets.length) return null
    const names = displayNames.slice(0, heatRows)
    const buckets = heatSeries.buckets.slice(startCol)
    const live = phase === 'live'
    // 领涨轨迹: 每列 top 行 (琥珀描边) 与走势线模式的易主圆点同一数据源
    const totalCols = heatSeries.buckets.length
    const topRows = topRowPerCol(computeLeaderReigns(heatSeries.matrix, startCol), totalCols)
    const cells: { value: [number, number, number | null]; itemStyle?: { borderColor: string; borderWidth: number; shadowColor?: string; shadowBlur?: number } }[] = []
    const absValues: number[] = []
    for (let row = 0; row < names.length; row++) {
      const values = heatSeries.matrix[row] ?? []
      for (let col = startCol; col < heatSeries.buckets.length; col++) {
        const value = values[col] ?? null
        // 盘中最后一列为未完成桶, 白描边提示数据未封口 (琥珀留给领涨轨迹);
        // 领涨轨迹用亮琥珀+微辉光: 半透明琥珀在深红/深绿格上几乎不可见
        const liveLast = live && col - startCol === cols - 1
        const isTopRow = topRows[col] === row
        cells.push({
          value: [col - startCol, row, value],
          itemStyle: liveLast
            ? { borderColor: 'rgba(255,255,255,0.95)', borderWidth: 1.6 }
            : isTopRow
              ? { borderColor: '#FCD34D', borderWidth: 1.5, shadowColor: 'rgba(252,211,77,0.5)', shadowBlur: 3 }
              : undefined,
        })
        if (value != null && value !== 0) absValues.push(Math.abs(value))
      }
    }
    if (!cells.length) return null
    // 色标按 |涨幅| 的 90 分位钳制: 个别小板块单桶 ±10% 会把绝对最大值撑爆,
    // 常见 ±0.5% 的波动就会近乎透明; 超出范围的颜色由 ECharts 饱和到端点
    absValues.sort((a, b) => a - b)
    const p90 = absValues.length ? absValues[Math.min(absValues.length - 1, Math.floor(absValues.length * 0.9))] : 0
    const maxAbs = Math.max(p90, 0.002)

    const rankByName = new Map((data?.sectors ?? []).map(item => [item.name, item]))
    return {
      grid: { left: 86, right: 14, top: 8, bottom: 46 },
      tooltip: {
        backgroundColor: chartTheme.tooltipBg,
        borderColor: chartTheme.tooltipBorder,
        textStyle: { color: chartTheme.tooltipText, fontSize: 10 },
        formatter: (params: unknown) => {
          const point = (Array.isArray(params) ? params : [params])[0] as {
            value?: [number, number, number | null]
          }
          const cell = point?.value
          if (!cell) return ''
          const [x, y, v] = cell
          const name = names[y]
          if (name === undefined) return ''
          const sector = rankByName.get(name)
          const rankPart = sector?.rank_now ? ` · 现排名 #${sector.rank_now}` : ''
          const changePart = sector?.rank_change ? ` (${sector.rank_change > 0 ? '↑' : '↓'}${Math.abs(sector.rank_change)})` : ''
          const liveHint = phase === 'live' && x === cols - 1 ? '<br/><span style="color:#f59e0b">进行中的桶 · 数据未封口</span>' : ''
          return [
            `<b>${name}</b>${rankPart}${changePart}`,
            `${buckets[x]} 桶涨幅: ${v == null ? '无数据' : fmtPct(v)}`,
            `当前涨幅 ${fmtPct(sector?.pct_now)} · 热度 ${sector?.score?.toFixed(0) ?? '—'}`,
          ].join('<br/>') + liveHint
        },
      },
      xAxis: {
        type: 'category', data: buckets,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: {
          fontSize: 9, color: chartTheme.text, hideOverlap: true,
          interval: Math.max(0, Math.ceil(buckets.length / 8) - 1),
        },
      },
      yAxis: {
        type: 'category', data: names, inverse: true,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { fontSize: 11, color: chartTheme.textStrong, formatter: (value: string) => (value.length > 8 ? `${value.slice(0, 8)}…` : value) },
      },
      visualMap: {
        type: 'continuous', min: -maxAbs, max: maxAbs,
        orient: 'horizontal', left: 'center', bottom: 0,
        itemWidth: 8, itemHeight: 110, text: ['涨', '跌'],
        textStyle: { fontSize: 9, color: chartTheme.text },
        // 中间为可见中性色: 平淡读作「平」而非「缺数据」; 两端饱和拉开红绿对比
        inRange: { color: ['#14532D', '#22C55E', '#323945', '#F87171', '#B91C1C'] },
        calculable: false,
      },
      series: [{
        name: '热度热力图', type: 'heatmap',
        data: cells,
        // 格间细缝: 无间隙的大色块会糊成一片, 半透明网格线让每格独立可辨
        itemStyle: { borderWidth: 1.5, borderColor: 'rgba(128,140,160,0.22)' },
        emphasis: { itemStyle: { borderColor: '#fbbf24', borderWidth: 2 } },
      }],
    }
  }, [data, bucket, chartTheme, phase, startCol, cols, displayNames, heatRows])
  const heat = useEChart(heatOption, {
    onMouseOver: (params) => {
      if (params.seriesType !== 'heatmap') return
      const value = params.value as [number, number, number | null] | undefined
      const y = value?.[1]
      setHoverName(y != null ? heatNames[y] ?? null : null)
    },
    onMouseOut: () => setHoverName(null),
    onGlobalOut: () => setHoverName(null),
  }, displayMode)

  // 联动反向: 榜单行悬停 → 展示图对应元素高亮 (热力图整行描边 / 走势线整线增亮)
  const prevHoverRef = useRef<{ mode: 'trend' | 'heatmap'; index: number } | null>(null)
  useEffect(() => {
    const inst = (displayMode === 'trend' ? trend.instRef : heat.instRef).current
    if (!inst || !data || data.status !== 'ok') return
    const clearPrev = () => {
      const prev = prevHoverRef.current
      if (!prev) return
      if (prev.mode === 'heatmap') {
        inst.dispatchAction({
          type: 'downplay', seriesIndex: 0,
          dataIndex: Array.from({ length: cols }, (_, c) => prev.index * cols + c),
        })
      } else if (prev.index >= 0) {
        inst.dispatchAction({ type: 'downplay', seriesIndex: prev.index })
      }
    }
    clearPrev()
    prevHoverRef.current = null
    if (!hoverName) return
    const y = displayNames.indexOf(hoverName)
    if (y < 0) return
    if (displayMode === 'heatmap') {
      inst.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: Array.from({ length: cols }, (_, c) => y * cols + c) })
      prevHoverRef.current = { mode: 'heatmap', index: y }
    } else {
      inst.dispatchAction({ type: 'highlight', seriesIndex: y })
      prevHoverRef.current = { mode: 'trend', index: y }
    }
  }, [displayMode, hoverName, data, bucket, cols, displayNames, heat.instRef, trend.instRef])

  // 强度图: 黄=切换强度 (左轴), 灰虚线=全市场, 蓝线=指数 (右轴 %); 窗口与展示图一致;
  // 领涨易主时点以 markPoint 圆点+板块名标注: 圆点颜色与走势线同源 (colorForSector,
  // 上下两图同一板块同色), 强度改由圆点大小表达 — 最强 3 次大点 / 最弱 2 次小点
  // (line series 设 symbol:'none' 后 data item 的 symbol 不生效, 只能走 markPoint)
  const chartOption = useMemo<echarts.EChartsOption | null>(() => {
    if (!timeline.length) return null
    const winBuckets = timeline.slice(startCol).map(point => point.time)
    if (!winBuckets.length) return null
    // 全天铺轴 (与走势图同一规则, 两图指针联动依赖轴窗口一致)
    const axisBuckets = startCol === 0 ? mergeFullDayBuckets(winBuckets, bucket) : winBuckets
    const dataIndexOf = new Map(winBuckets.map((label, i) => [label, i] as const))
    const switchIndices: number[] = []
    for (let index = 1; index < timeline.length; index++) {
      if (timeline[index - 1].leader !== timeline[index].leader) switchIndices.push(index)
    }
    // 事件集 = 窗口内易主 + 窗口开局领涨 (走势图对开局领涨一直有圆点标注,
    // 切换图不标会"少一个事件" — 开盘即领涨的板块整段没有任何标记)
    const eventIndices = [startCol, ...switchIndices.filter(index => index > startCol)]
    const byRotation = [...eventIndices].sort(
      (a, b) => timeline[a].rotation - timeline[b].rotation,
    )
    const highIdx = new Set(byRotation.slice(-3))
    const lowIdx = new Set(byRotation.slice(0, 2))
    // 易主标注点: 颜色=接管板块的走势线色, 大小=切换强度; formatter 用函数式 —
    // markPoint data item 的字符串 formatter 会让整个 markPoint 不渲染。
    // 标签防挤叠 (像素级 AABB): 按绘图区几何估算每个标签的包围盒 (上/下随侧别
    // 偏移), 与已放置标签相交则隐藏本标签只留圆点 — 低点的 top 标签和高点的
    // bottom 标签会收敛到同一水平线, 按圆点数值差判断拦不住, 必须按标签落位判断。
    // 密集聚簇与其拼成一个词不如少一个装饰性标签 (tooltip 信息完整)。
    // 圆点加底色描边, 防小圆点贴在强度线上看不见。
    // 绘图区按 h-44 容器的保守值估算 (实际更宽只会更宽松, 宁可多隐藏不重叠)。
    const plotW = 380, plotH = 126
    const bucketW = plotW / Math.max(1, axisBuckets.length)
    const labelBox = (index: number, value: number, side: 'top' | 'bottom', textLen: number): number[] => {
      const x = (index - startCol) * bucketW
      const y = (1 - value) * plotH   // 数值轴 0 在底部
      const w = textLen * 9.5, h = 12
      // 各向外扩 2px 余量
      return side === 'top'
        ? [x - w / 2 - 2, y - 5 - h - 2, x + w / 2 + 2, y - 5 + 2]
        : [x - w / 2 - 2, y + 5 - 2, x + w / 2 + 2, y + 5 + h + 2]
    }
    const boxesOverlap = (a: number[], b: number[]) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
    const takeoverMarks: {
      name: string
      coord: [string, number]
      symbolSize: number
      itemStyle: { color: string; borderColor: string; borderWidth: number }
      label: Record<string, unknown>
    }[] = []
    const sideLastBucket: { top: number; bottom: number } = { top: -999, bottom: -999 }
    const placedBoxes: number[][] = []
    // 最强切换 (大点) 的标签信息量最高, 密集区先占位 — 弱事件标签撞上才被隐藏
    const orderedEvents = [
      ...eventIndices.filter(index => highIdx.has(index)),
      ...eventIndices.filter(index => !highIdx.has(index) && lowIdx.has(index)),
    ]
    for (const index of orderedEvents) {
      const point = timeline[index]
      const strong = highIdx.has(index)
      if (!strong && !lowIdx.has(index)) continue
      const color = colorForSector(point.leader)
      const prefer: 'top' | 'bottom' = takeoverMarks.length % 2 ? 'bottom' : 'top'
      const alt: 'top' | 'bottom' = prefer === 'top' ? 'bottom' : 'top'
      const side = index - sideLastBucket[prefer] < 6
        ? (index - sideLastBucket[alt] < 6 ? prefer : alt)
        : prefer
      sideLastBucket[side] = index
      const box = labelBox(index, point.rotation, side, point.leader.length)
      const showLabel = placedBoxes.every(placed => !boxesOverlap(box, placed))
      if (showLabel) placedBoxes.push(box)
      takeoverMarks.push({
        name: point.leader,
        coord: [point.time, point.rotation],
        symbolSize: strong ? 8 : 5,
        itemStyle: { color, borderColor: chartTheme.tooltipBg, borderWidth: 1 },
        label: {
          show: showLabel, formatter: () => point.leader,
          position: side,
          distance: 5,
          fontSize: 9.5, color, textBorderColor: chartTheme.tooltipBg, textBorderWidth: 2,
        },
      })
    }
    const onAxis = (pick: (point: (typeof timeline)[number], absIdx: number) => number | null) =>
      axisBuckets.map(label => {
        const winIdx = dataIndexOf.get(label)
        return winIdx === undefined ? null : pick(timeline[startCol + winIdx], startCol + winIdx)
      })
    return {
      grid: { left: 38, right: 46, top: 26, bottom: 24 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: chartTheme.tooltipBg,
        borderColor: chartTheme.tooltipBorder,
        textStyle: { color: chartTheme.tooltipText, fontSize: 10 },
        formatter: (params: unknown) => {
          const list = (Array.isArray(params) ? params : [params]) as Array<{ axisValue?: string }>
          const axisValue = list[0]?.axisValue ?? ''
          const idx = dataIndexOf.get(axisValue)
          const point = idx === undefined ? undefined : timeline[startCol + idx]
          if (!point) return `<b>${axisValue}</b>`
          const lines = [
            `<b>${point.time}</b>`,
            `切换强度 ${point.rotation.toFixed(2)}`,
            `领涨${dimLabel} ${point.leader} (${fmtPct(point.leader_pct)})`,
            `全市场 ${fmtPct(point.market_pct)}`,
          ]
          const idxVal = indexLine && idx !== undefined ? indexLine.points[startCol + idx] : null
          if (indexLine && idxVal != null) lines.push(`${indexLine.label} ${fmtPct(idxVal)}`)
          if (phase === 'live' && idx === cols - 1) lines.push('<span style="color:#f59e0b">进行中的桶 · 数据未封口</span>')
          return lines.join('<br/>')
        },
      },
      xAxis: {
        type: 'category', data: axisBuckets,
        axisLabel: {
          fontSize: 9, color: chartTheme.text, hideOverlap: true,
          interval: Math.max(0, Math.ceil(axisBuckets.length / 6) - 1),
          showMaxLabel: true,
        },
      },
      yAxis: [
        { type: 'value', min: 0, max: 1, axisLabel: { fontSize: 9, color: chartTheme.text }, splitLine: { lineStyle: { color: 'rgba(128,140,160,0.15)' } } },
        { type: 'value', axisLabel: { fontSize: 9, color: chartTheme.text, formatter: (v: number) => `${(v * 100).toFixed(1)}%` }, splitLine: { show: false } },
      ],
      series: [
        {
          name: '切换强度', type: 'line', smooth: true, symbol: 'none',
          data: onAxis(point => point.rotation),
          lineStyle: { color: '#f59e0b', width: 1.6 },
          areaStyle: { color: 'rgba(245,158,11,0.12)' },
          connectNulls: true,
          ...(takeoverMarks.length ? { markPoint: { symbol: 'circle', silent: true, data: takeoverMarks } } : {}),
        },
        {
          name: '全市场', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 1,
          data: onAxis(point => point.market_pct),
          lineStyle: { color: 'rgba(128,140,160,0.55)', width: 1, type: 'dashed' },
          connectNulls: true,
        },
        ...(indexLine ? [{
          name: indexLine.label, type: 'line' as const, smooth: true, symbol: 'none', yAxisIndex: 1,
          data: onAxis((_point, absIdx) => indexLine.points[absIdx] ?? null),
          lineStyle: { color: '#60a5fa', width: 1.4 },
          connectNulls: true,
        }] : []),
      ],
    }
  }, [data, dimLabel, indexLine, timeline, startCol, cols, phase, chartTheme, bucket])
  const chart = useEChart(chartOption)

  // 强度图与展示图 x 轴指针跨实例联动 (两图 x 轴窗口一致才可对齐)
  useEffect(() => {
    const a = chart.instRef.current
    const b = (displayMode === 'trend' ? trend.instRef : heat.instRef).current
    if (!a || !b) return
    a.group = CHART_CONNECT_GROUP
    b.group = CHART_CONNECT_GROUP
    echarts.connect(CHART_CONNECT_GROUP)
  }, [data, bucket, displayMode, chart.instRef, trend.instRef, heat.instRef])

  const onRowsModeChange = (value: SectorSource) => {
    setRowsMode(value)
    localStorage.setItem(`${ROWS_MODE_PREFIX}${kind}`, value)
  }
  const onFlowChange = (value: string) => {
    setFlow(value)
    if (value) localStorage.setItem(`${FLOW_LS_PREFIX}${kind}`, value)
    else {
      localStorage.removeItem(`${FLOW_LS_PREFIX}${kind}`)
      // 资金流维度依赖扩展列数据源: 清空列后该维度失效, 自动回退综合分
      if (rowsMode === 'flow') onRowsModeChange('score')
    }
  }
  const onDisplayModeChange = (value: 'trend' | 'heatmap') => {
    setDisplayMode(value)
    localStorage.setItem(`${DISPLAY_MODE_PREFIX}${kind}`, value)
  }
  const toggleCustom = (name: string) => {
    if (customNames.includes(name)) {
      const next = customNames.filter(item => item !== name)
      setCustomNames(next)
      localStorage.setItem(`${CUSTOM_NAMES_PREFIX}${kind}`, JSON.stringify(next))
      return
    }
    if (customNames.length >= MAX_CUSTOM_SECTORS) {
      toast(`最多监控 ${MAX_CUSTOM_SECTORS} 个板块`, 'error')
      return
    }
    const next = [...customNames, name]
    setCustomNames(next)
    localStorage.setItem(`${CUSTOM_NAMES_PREFIX}${kind}`, JSON.stringify(next))
  }

  const onAutoRowsChange = (value: number) => {
    setAutoRows(value)
    localStorage.setItem(`${AUTO_ROWS_PREFIX}${kind}`, String(value))
  }
  // null = 移除自定义, 回到后端内置名单
  const persistExclude = (next: string[] | null) => {
    setExcludeNames(next)
    if (next === null) localStorage.removeItem(`${EXCLUDE_PREFIX}${kind}`)
    else localStorage.setItem(`${EXCLUDE_PREFIX}${kind}`, JSON.stringify(next))
  }
  const removeExclude = (name: string) => {
    persistExclude(effectiveExclude.filter(item => item !== name))
  }
  const addExclude = () => {
    const name = excludeInput.trim()
    if (!name) return
    if (effectiveExclude.includes(name)) {
      setExcludeInput('')
      return
    }
    if (effectiveExclude.length >= MAX_EXCLUDE_SECTORS) {
      toast(`排除名单最多 ${MAX_EXCLUDE_SECTORS} 项`, 'error')
      return
    }
    persistExclude([...effectiveExclude, name])
    setExcludeInput('')
  }

  const latest = data?.timeline?.[data.timeline.length - 1]
  const heatHeight = Math.max(180, heatRows * HEAT_ROW_HEIGHT + 62)
  // 左列是否渲染 (走势线无板块 / 热力图无行时缺位, 右列全宽补上)
  const showLeftChart = displayMode === 'heatmap' ? heatRows > 0 : displayNames.length > 0

  // 未开启全量分钟落盘时整卡隐藏 (整卡空态占位过大, 白占首屏);
  // 其余 reason (当日数据为空/成分缺失) 属"有能力缺数据", 保留引导文案
  if (data && data.status !== 'ok' && data.reason === 'minute_missing') {
    return null
  }

  return (
    <section className="rounded-2xl border border-border bg-surface p-2.5">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-amber-400 to-amber-400/30" />
        <Activity className="h-3.5 w-3.5 text-amber-500" />
        <h2 className="text-xs font-semibold text-foreground">{dimLabel}切换 · 盘中轮动</h2>
        {data?.status === 'ok' && data.date && (
          phase === 'live' ? (
            <span className="inline-flex items-center gap-1 rounded bg-emerald-400/10 px-1.5 py-0.5 text-[9px] font-medium text-emerald-400">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />盘中
            </span>
          ) : (
            <span className="rounded bg-elevated px-1.5 py-0.5 text-[9px] text-muted">回放 · {data.date}</span>
          )
        )}
        <span className="text-[10px] text-muted">
          {data?.status === 'ok' && latest ? (
            <>
              {`${data.date} ${data.as_of} · 切换强度 ${latest.rotation.toFixed(2)} · `}
              <span className="font-medium text-amber-400/90">领涨 {latest.leader}</span>
            </>
          ) : '全量分钟聚合'}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {/* 显示模式: 胶囊分段控件, 只与刷新同层 — 数据塑形控件分离到下一行 */}
          <div className="inline-flex items-center rounded-md bg-base/60 p-0.5" role="group" aria-label="显示模式">
            {([['trend', '走势线'], ['heatmap', '热力图']] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => onDisplayModeChange(value)}
                className={cn(
                  'rounded px-2.5 py-0.5 text-[11px] transition-colors',
                  displayMode === value ? 'bg-surface font-medium text-accent shadow-sm ring-1 ring-accent/30' : 'text-muted hover:text-foreground',
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <RefreshCw className={`h-3 w-3 text-muted ${rotationQuery.isFetching ? 'animate-spin text-accent' : ''}`} />
        </div>
      </div>

      {/* 数据塑形工具行: 来源 / 行数 / 过滤 / 更多 — 与标题状态分层, 不再一行塞满 */}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <select
          aria-label="板块来源"
          className="h-7 rounded border border-border bg-surface px-1.5 text-[11px] text-secondary outline-none focus:border-accent"
          value={rowsMode}
          onChange={event => onRowsModeChange(event.target.value as SectorSource)}
        >
          <option value="activity">活跃度</option>
          <option value="score">综合分</option>
          <option value="pct">现涨幅</option>
          <option value="rank_change">切入</option>
          <option value="momentum">强弱切换</option>
          {/* 资金流维度依赖扩展列数据源: 未选列时不提供, 避免静默退化成综合分 */}
          {flow && <option value="flow">资金流</option>}
          <option value="custom">自定义</option>
        </select>
        {rowsMode !== 'custom' && (
          <>
            <select
              aria-label="活跃榜行数"
              className="h-7 rounded border border-border bg-surface px-1.5 text-[11px] text-secondary outline-none focus:border-accent"
              value={autoRows}
              onChange={event => onAutoRowsChange(Number(event.target.value))}
            >
              {AUTO_ROW_OPTIONS.map(value => <option key={value} value={value}>前{value}</option>)}
            </select>
            <button
              type="button"
              onClick={() => setExcludeEditorOpen(v => !v)}
              aria-label="编辑自动活跃榜排除名单"
              className={cn(
                'h-7 rounded border px-2 text-[11px]',
                excludeEditorOpen ? 'border-accent text-accent' : 'border-border text-secondary hover:text-accent',
              )}
            >
              过滤{excludeNames !== null ? `·${excludeNames.length}` : ''}
            </button>
          </>
        )}
        <button
          type="button"
          onClick={() => setAdvancedOpen(v => !v)}
          aria-label="更多图表选项 (指数叠加 / 资金流 / 分钟粒度)"
          title="指数叠加 / 资金流 / 分钟粒度"
          className={cn(
            'inline-flex h-7 items-center gap-1 rounded border px-2 text-[11px]',
            advancedOpen ? 'border-accent text-accent' : 'border-border text-secondary hover:text-accent',
          )}
        >
          <SlidersHorizontal className="h-3 w-3" />
          更多
        </button>
      </div>

      {/* 次级选项折叠行: 指数叠加 / 资金流 / 分钟粒度 */}
      {advancedOpen && data?.status === 'ok' && (
        <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-border/60 bg-elevated/30 px-2 py-1.5">
          <label className="flex items-center gap-1.5 text-[10px] text-muted">
            指数叠加
            <select
              aria-label="指数叠加"
              className="h-7 max-w-28 truncate rounded border border-border bg-surface px-1.5 text-[11px] text-secondary outline-none focus:border-accent"
              value={indexSymbol}
              onChange={event => onIndexChange(event.target.value)}
            >
              {CORE_INDEXES.map(item => <option key={item.symbol} value={item.symbol}>{item.name}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[10px] text-muted">
            资金流
            <select
              aria-label="资金流扩展列"
              className="h-7 max-w-56 truncate rounded border border-border bg-surface px-1.5 text-[11px] text-secondary outline-none focus:border-accent"
              value={flow}
              onChange={event => onFlowChange(event.target.value)}
            >
              <option value="">不使用</option>
              {flowOptions.map(option => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-[10px] text-muted">
            分钟桶
            <select
              aria-label="分钟桶粒度"
              className="h-7 rounded border border-border bg-surface px-1.5 text-[11px] text-secondary outline-none focus:border-accent"
              value={bucket}
              onChange={event => setBucket(Number(event.target.value))}
            >
              {[1, 5, 15].map(value => <option key={value} value={value}>{value}分钟</option>)}
            </select>
          </label>
        </div>
      )}

      {rotationQuery.isLoading ? (
        <div className="flex h-36 items-center justify-center text-xs text-muted">正在聚合全市场分钟数据…</div>
      ) : rotationQuery.isError ? (
        <div className="flex h-36 items-center justify-center text-xs text-danger">
          板块切换数据加载失败 · {String((rotationQuery.error as Error)?.message || rotationQuery.error)}
        </div>
      ) : !data || data.status !== 'ok' ? (
        <div className="flex h-36 items-center justify-center px-6 text-center text-xs text-muted">
          {NO_DATA_HINTS[data?.reason ?? ''] ?? '暂无板块切换数据 — 请先开启全量分钟能力并获取板块成分'}
        </div>
      ) : (
        <>
          {rowsMode !== 'custom' && excludeEditorOpen && (
            <div className="mb-2 rounded-lg border border-border/60 bg-elevated/30 p-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[9px] text-muted">
                  自动活跃榜排除板块 {effectiveExclude.length}/{MAX_EXCLUDE_SECTORS}
                  {data?.max_auto_members != null && ` · 成员数>${data.max_auto_members} 自动排除`}
                </span>
                <button
                  type="button"
                  onClick={() => persistExclude(null)}
                  disabled={excludeNames === null}
                  className="ml-auto rounded border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  恢复默认
                </button>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {effectiveExclude.map(name => (
                  <span key={name} className="inline-flex items-center gap-1 rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                    {name}
                    <button type="button" onClick={() => removeExclude(name)} className="cursor-pointer hover:text-danger" aria-label={`移除 ${name}`}>×</button>
                  </span>
                ))}
                {effectiveExclude.length === 0 && (
                  <span className="text-[9px] text-muted/70">名称过滤已清空 — 仅按成员数上限过滤</span>
                )}
              </div>
              <div className="mt-1.5 flex items-center gap-1.5">
                <input
                  value={excludeInput}
                  onChange={event => setExcludeInput(event.target.value)}
                  onKeyDown={event => { if (event.key === 'Enter') addExclude() }}
                  placeholder="添加要排除的板块名称 (名称包含即排除)"
                  className="h-6 flex-1 rounded border border-border bg-surface px-2 text-[10px] text-foreground outline-none focus:border-accent"
                />
                <button
                  type="button"
                  onClick={addExclude}
                  className="rounded border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-accent"
                >
                  添加
                </button>
              </div>
              <div className="px-1 pt-1 text-[9px] text-muted/70">
                仅影响自动活跃榜的选取 — 自定义监控不受影响; 过滤后不足展示行数时回退为不过滤
              </div>
            </div>
          )}
          {rowsMode === 'custom' && (
            <div className="mb-2 rounded-lg border border-border/60 bg-elevated/30 p-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[9px] text-muted">监控板块 {customNames.length}/{MAX_CUSTOM_SECTORS}</span>
                {customNames.map(name => (
                  <span key={name} className="inline-flex items-center gap-1 rounded bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                    {name}
                    <button type="button" onClick={() => toggleCustom(name)} className="cursor-pointer hover:text-danger" aria-label={`移除 ${name}`}>×</button>
                  </span>
                ))}
                {customNames.length === 0 && <span className="text-[9px] text-muted/70">未选择板块 — 暂按活跃榜展示</span>}
                <button
                  type="button"
                  onClick={() => setPickerOpen(v => !v)}
                  className="ml-auto rounded border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-accent"
                >
                  {pickerOpen ? '收起' : '＋添加板块'}
                </button>
              </div>
              {pickerOpen && (
                <div className="mt-1.5">
                  <input
                    value={pickerSearch}
                    onChange={event => setPickerSearch(event.target.value)}
                    placeholder="搜索板块名"
                    className="mb-1 h-6 w-full rounded border border-border bg-surface px-2 text-[10px] text-foreground outline-none focus:border-accent"
                  />
                  <div className="max-h-44 overflow-y-auto rounded border border-border/40">
                    {(data.universe ?? [])
                      .filter(item => item.name.includes(pickerSearch.trim()))
                      .map(item => {
                        const selected = customNames.includes(item.name)
                        return (
                          <button
                            key={item.name}
                            type="button"
                            onClick={() => toggleCustom(item.name)}
                            className={cn(
                              'flex w-full items-center justify-between gap-2 px-2 py-1 text-left text-[10px] hover:bg-elevated/50',
                              selected && 'bg-accent/5 text-accent',
                            )}
                          >
                            <span className="truncate">{selected ? '✓ ' : ''}{item.name}</span>
                            <span className="shrink-0 font-mono text-muted">
                              {fmtFlow(item.activity)} · {fmtPct(item.pct_now)}
                            </span>
                          </button>
                        )
                      })}
                    {!(data.universe ?? []).length && <div className="p-2 text-center text-[10px] text-muted">板块清单不可用</div>}
                  </div>
                  <div className="px-1 pt-1 text-[9px] text-muted/70">右列为 活跃度 (近 30 分钟成交额合计) · 现涨幅</div>
                </div>
              )}
            </div>
          )}

          {/* 宽屏左右并排 (issue #518 #2): 左=展示图 (aspect-ratio 随列宽, ~1.8:1,
              不再全宽 4.4:1 压扁), 右=切换强度图+榜单; 窄屏退回上下堆叠。
              右列内层 xl 下绝对定位脱离 grid 行高计算 — 30 行榜单不再把行顶到
              1100px+, 榜单列内 flex-1 内部滚动, 两列底部齐平。 */}
          <div className="grid grid-cols-1 gap-2 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
            {displayMode === 'heatmap' ? (
              heatRows > 0 && (
                // key 强制模式切换时销毁重建容器: 两个分支同为 <div> 时 React 会原地
                // 复用节点换挂 ref, 造成两个 echarts 实例交叉挤占同一个 dom
                <div key="heatmap" className="rounded-lg border border-border/60 bg-elevated/30 p-1.5">
                  <Legend
                    items={[
                      { swatch: swatchBlock('#B91C1C'), label: '涨' },
                      { swatch: swatchBlock('#323945'), label: '平' },
                      { swatch: swatchBlock('#14532D'), label: '跌' },
                      { swatch: swatchFrame('#FCD34D'), label: '领涨轨迹 (该桶最强)' },
                      ...(phase === 'live'
                        ? [{ swatch: swatchFrame('rgba(255,255,255,0.9)'), label: '未封口桶' }]
                        : []),
                      { label: '悬停联动榜单' },
                    ]}
                  />
                  <div ref={heat.ref} style={{ height: heatHeight }} className="w-full" />
                </div>
              )
            ) : (
              displayNames.length > 0 && (
                <div key="trend" className="rounded-lg border border-border/60 bg-elevated/30 p-1.5">
                  <Legend
                    items={[
                      { swatch: swatchLine('#60a5fa'), label: '线端常显板块名 (悬停聚焦)' },
                      { swatch: swatchDot('#fbbf24'), label: '圆点=领涨易主 · 标注接管板块' },
                      { swatch: swatchDash('rgba(128,140,160,0.9)'), label: '0% 轴: 上穿=切入 / 下穿=退潮' },
                    ]}
                  />
                  {/* 高度随列宽 (9:5): 固定像素高在宽窗口会压成 4:1 扁条; minHeight 兜底窄列 */}
                  <div ref={trend.ref} style={{ aspectRatio: '9 / 5', minHeight: 300 }} className="w-full" />
                </div>
              )
            )}
            <div className={cn('relative xl:min-h-0', !showLeftChart && 'xl:col-span-2')}>
              <div className="flex flex-col gap-2 xl:absolute xl:inset-0">
                {/* 涨跌切换 (0轴穿越) — 独立分组: 可见窗口累计 + 最新事件名单,
                    悬停名称联动上图/榜单定位 */}
                <div className="rounded-lg border border-border/60 bg-elevated/30 p-1.5">
                  <Legend
                    items={[
                      { swatch: swatchLine('#f59e0b'), label: '切换强度 (左轴)' },
                      { swatch: swatchDot('#f59e0b'), label: '圆点=领涨易主(含开局) · 颜色同走势线 · 大点=最强切换' },
                      ...(indexLine
                        ? [{ swatch: swatchLine('#60a5fa'), label: `${indexLine.label} (右轴)` }]
                        : []),
                      { swatch: swatchDash('rgba(128,140,160,0.8)'), label: '全市场 (右轴) · 与展示图指针联动' },
                    ]}
                  />
                  <div ref={chart.ref} className="h-44 w-full" />
                </div>
                <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border/60">
                  <div className="grid grid-cols-[minmax(0,1.4fr)_64px_64px_58px_minmax(72px,1fr)] border-b border-border bg-base/50 px-2 py-1.5 text-[9px] font-medium text-muted">
                    <span>{dimLabel}</span><span className="text-right">现涨幅</span><span className="text-right">1h前</span><span className="text-right">排名变化</span><span className="text-right">{data.flow_available ? '资金流 / 综合分' : '综合分'}</span>
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    {data.sectors.map((sector: SectorRotationSector) => (
                      <div
                        key={sector.name}
                        onMouseEnter={() => setHoverName(sector.name)}
                        onMouseLeave={() => setHoverName(null)}
                        className={cn(
                          'grid grid-cols-[minmax(0,1.4fr)_64px_64px_58px_minmax(72px,1fr)] items-center border-b border-border/40 px-2 py-1.5 text-[10px] last:border-b-0 hover:bg-elevated/40',
                          hoverName === sector.name && 'bg-accent/10',
                        )}
                      >
                        <span className="truncate font-medium text-foreground" title={`${sector.name} · 成分 ${sector.n_members_with_bars}/${sector.n_members}`}>
                          <span
                            aria-hidden
                            className="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle"
                            style={{
                              // 与走势线/易主圆点同一稳定配色; 未进矩阵的板块给灰色占位点
                              backgroundColor: displayNames.includes(sector.name)
                                ? colorForSector(sector.name)
                                : SECTOR_DOT_UNMATCHED,
                            }}
                          />
                          {sector.name}
                        </span>
                        <span className={`text-right font-mono ${pctClass(sector.pct_now)}`}>{fmtPct(sector.pct_now)}</span>
                        <span className={`text-right font-mono ${pctClass(sector.pct_prev)}`}>{fmtPct(sector.pct_prev)}</span>
                        <span className={`text-right font-mono ${sector.rank_change == null ? 'text-muted' : sector.rank_change > 0 ? 'text-bull' : sector.rank_change < 0 ? 'text-bear' : 'text-muted'}`}>
                          {sector.rank_change == null ? '—' : sector.rank_change > 0 ? `↑${sector.rank_change}` : sector.rank_change < 0 ? `↓${-sector.rank_change}` : '—'}
                        </span>
                        <span className="truncate text-right font-mono text-secondary" title={data.flow_available ? `资金流 ${fmtFlow(sector.flow)}` : '未选择资金流, 综合分=涨幅归一'}>
                          {data.flow_available ? `${fmtFlow(sector.flow)} · ${sector.score?.toFixed(0) ?? '—'}` : `${sector.score?.toFixed(0) ?? '—'}分`}
                        </span>
                      </div>
                    ))}
                    {!data.sectors.length && <div className="p-3 text-center text-[10px] text-muted">暂无板块数据</div>}
                  </div>
                </div>
              </div>
            </div>
          </div>
          {/* 涨跌切换 (0 轴穿越) 事件带 — 全宽, 名称悬停联动上图/榜单定位 */}
          {(() => {
            const win = timeline.slice(startCol)
            const upTotal = win.reduce((sum, point) => sum + (point.cross_up ?? 0), 0)
            const downTotal = win.reduce((sum, point) => sum + (point.cross_down ?? 0), 0)
            const latest = win[win.length - 1]
            const events = [...(data.cross_events ?? [])]
            const upNames = events.filter(event => event.dir === 'up').slice(0, 8)
            const downNames = events.filter(event => event.dir === 'down').slice(0, 8)
            return (
              <div className="mt-2 rounded-lg border border-border/60 bg-elevated/30 p-1.5">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px]">
                  <span className="font-medium text-foreground">涨跌切换 (0轴穿越)</span>
                  <span className="text-muted">可见窗口累计</span>
                  <span className="text-bull">↑ 转强 {upTotal}</span>
                  <span className="text-bear">↓ 转弱 {downTotal}</span>
                  {latest && (latest.cross_up || latest.cross_down) ? (
                    <span className="text-muted">最新桶 ↑{latest.cross_up ?? 0} / ↓{latest.cross_down ?? 0}</span>
                  ) : null}
                  <span className="ml-auto text-[9px] text-muted">悬停名称可在上图 / 榜单定位</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px]">
                  <span className="w-12 shrink-0 text-bull">↑ 转强</span>
                  {upNames.map(event => (
                    <button
                      key={`${event.time}-${event.name}-up`}
                      type="button"
                      onMouseEnter={() => setHoverName(event.name)}
                      onMouseLeave={() => setHoverName(null)}
                      className="rounded bg-base/60 px-1.5 py-0.5 text-bull hover:bg-elevated/60"
                    >
                      {event.name} <span className="font-mono text-[9px] opacity-60">{event.time}</span>
                    </button>
                  ))}
                  {!upNames.length && <span className="text-muted/70">无</span>}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1 text-[10px]">
                  <span className="w-12 shrink-0 text-bear">↓ 转弱</span>
                  {downNames.map(event => (
                    <button
                      key={`${event.time}-${event.name}-down`}
                      type="button"
                      onMouseEnter={() => setHoverName(event.name)}
                      onMouseLeave={() => setHoverName(null)}
                      className="rounded bg-base/60 px-1.5 py-0.5 text-bear hover:bg-elevated/60"
                    >
                      {event.name} <span className="font-mono text-[9px] opacity-60">{event.time}</span>
                    </button>
                  ))}
                  {!downNames.length && <span className="text-muted/70">无</span>}
                </div>
              </div>
            )
          })()}
          <div className="mt-1.5 flex items-center gap-1.5 text-[9px] text-muted">
            <Database className="h-3 w-3" />
            {`${data.member_count} 个${dimLabel} · ${data.bucket_minutes}分钟桶 · 基准 ${data.basis === 'prev_close' ? '昨收' : data.basis === 'first_close' ? '今开' : '混合'}`}
            {data.flow_available ? ` · 资金流 ${data.flow_field}` : ' · 未启用资金流'}
            <span className="ml-auto">
              {rowsMode === 'custom'
                ? `自定义监控 ${customNames.length} 个`
                : `${SOURCE_LABELS[rowsMode]}前 ${displayNames.length}${rowsMode === 'activity' ? ' (近 30 分钟成交额)' : rowsMode === 'momentum' ? ' (走强→走弱)' : ''}`} · 每 30s 自动刷新 · 排名变化 ↑切入 ↓退潮 (相对 1 小时前)
            </span>
          </div>
        </>
      )}
    </section>
  )
}
