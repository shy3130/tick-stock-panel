/**
 * 板块切换卡片 (盘中轮动) — 概念/行业分析页共用。
 *
 * 数据: GET /api/sector-rotation (全量分钟聚合, 概念/行业二选一由页面 kind 决定),
 * 30s 前端轮询实时刷新 (分钟数据后端 6s 增量落盘, 30s 粒度已足够盘中观察)。
 * 资金流维度来自用户选择的扩展数据列 (ext schema-all 动态列出数值列, 不写死),
 * 选择按 kind 持久化到 localStorage。指数叠加线来自 /api/index/* (核心四只)。
 * 布局: 热力图全宽在上; 下方左=切换强度线, 右=板块榜单。
 * 两张图 x 轴窗口一致, 经 echarts.connect 联动指针; 榜单行与热力图行双向高亮。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Activity, Database, RefreshCw } from 'lucide-react'
import { api, type SectorRotationSector } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { cn } from '@/lib/cn'
import { CORE_INDEXES } from '@/components/Layout'

const FLOW_LS_PREFIX = 'sector_rotation_flow_'
const INDEX_LS_PREFIX = 'sector_rotation_index_'
const NUMERIC_TYPES = new Set(['float', 'int', 'number', 'double', 'long'])
// 强度图与热力图跨实例联动分组 (x 轴指针同步)
const CHART_CONNECT_GROUP = 'sector-rotation-card'

// 热力图: 行 = 热度降序板块; 1 分钟桶全天 240+ 列不可读, 只看最近 60 列
const HEAT_ROWS = 12
const HEAT_COLS_1M = 60
const HEAT_ROW_HEIGHT = 24

interface HeatEventParams {
  seriesType?: string
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
      // 容器随 loading 分支被卸载: 旧实例绑在已脱离 DOM 的节点上, 直接释放
      instRef.current?.dispose()
      instRef.current = null
      return
    }
    // 数据键切换会走 loading 分支卸载图表容器再重挂, 组件本身不卸载,
    // 旧实例会绑在脱离 DOM 的旧节点上 — 检测到 dom 不一致必须弃旧重建,
    // 否则 setOption 画进离屏节点, 图表永久空白 (切 1/5/15 分钟桶复现)
    if (instRef.current && instRef.current.getDom() !== ref.current) {
      instRef.current.dispose()
      instRef.current = null
    }
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
  }, [option])
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
        if (!NUMERIC_TYPES.has(String(column.type).toLowerCase())) continue
        options.push({
          value: `${item.id}.${column.name}`,
          label: `${item.label || item.id} · ${column.label || column.name}`,
        })
      }
    }
    return options
  }, [schemaQuery.data])

  const rotationQuery = useQuery({
    queryKey: QK.sectorRotation(kind, flow, bucket),
    queryFn: () => api.sectorRotation({ kind, flow: flow || undefined, bucket }),
    refetchInterval: 30_000,
    staleTime: 25_000,
  })
  const data = rotationQuery.data

  // 指数叠加线 (核心四只, 默认上证): 分钟桶涨幅对齐轮动时间轴, 昨收基准
  const [indexSymbol, setIndexSymbol] = useState<string>(
    () => localStorage.getItem(`${INDEX_LS_PREFIX}${kind}`) ?? CORE_INDEXES[0].symbol,
  )
  const indexLabel = CORE_INDEXES.find(item => item.symbol === indexSymbol)?.name ?? indexSymbol
  const indexMinuteQuery = useQuery({
    queryKey: QK.sectorRotationIndexMinute(indexSymbol, data?.date),
    queryFn: () => api.indexMinute(indexSymbol, data!.date!),
    enabled: !!data?.date,
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

  // 盘中/回放: 轮动日期 == 北京今天且处于连续竞价时段 → 盘中 (仅在连续竞价时
  // 最后一桶才是"未封口"的; 午休/盘前/收盘后一律按回放呈现)
  const phase: 'live' | 'replay' = useMemo(() => {
    if (!data || data.status !== 'ok' || !data.date) return 'replay'
    const { date: today } = beijingDateParts()
    if (data.date !== today) return 'replay'
    return isMarketSessionNow() ? 'live' : 'replay'
  }, [data])

  // 两张图共享同一 x 轴窗口 (1 分钟桶只看最近 60 列), 经 echarts.connect 联动指针
  const timeline = data?.timeline ?? []
  const total = timeline.length
  const startCol = bucket === 1 ? Math.max(0, total - HEAT_COLS_1M) : 0
  const cols = Math.max(0, total - startCol)
  const heatNames = (data?.series?.sectors ?? []).slice(0, HEAT_ROWS)
  const heatRows = Math.min(HEAT_ROWS, data?.series?.sectors.length ?? 0)

  const chartTheme = useChartTheme()

  // 强度图: 黄=切换强度 (左轴), 灰虚线=全市场, 蓝线=指数 (右轴 %); 窗口与热力图一致
  const chartOption = useMemo<echarts.EChartsOption | null>(() => {
    if (!timeline.length) return null
    const buckets = timeline.slice(startCol).map(point => point.time)
    if (!buckets.length) return null
    return {
      grid: { left: 38, right: 46, top: 10, bottom: 20 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: chartTheme.tooltipBg,
        borderColor: chartTheme.tooltipBorder,
        textStyle: { color: chartTheme.tooltipText, fontSize: 10 },
        formatter: (params: unknown) => {
          const list = (Array.isArray(params) ? params : [params]) as Array<{ axisValue?: string }>
          const idx = buckets.indexOf(list[0]?.axisValue ?? '')
          const point = timeline[startCol + idx]
          if (!point) return ''
          const lines = [
            `<b>${point.time}</b>`,
            `切换强度 ${point.rotation.toFixed(2)}`,
            `领涨${dimLabel} ${point.leader} (${fmtPct(point.leader_pct)})`,
            `全市场 ${fmtPct(point.market_pct)}`,
          ]
          const idxVal = indexLine ? indexLine.points[startCol + idx] : null
          if (indexLine && idxVal != null) lines.push(`${indexLine.label} ${fmtPct(idxVal)}`)
          if (phase === 'live' && idx === cols - 1) lines.push('<span style="color:#f59e0b">进行中的桶 · 数据未封口</span>')
          return lines.join('<br/>')
        },
      },
      xAxis: { type: 'category', data: buckets, axisLabel: { fontSize: 9, color: chartTheme.text } },
      yAxis: [
        { type: 'value', min: 0, max: 1, axisLabel: { fontSize: 9, color: chartTheme.text }, splitLine: { lineStyle: { color: 'rgba(128,140,160,0.15)' } } },
        { type: 'value', axisLabel: { fontSize: 9, color: chartTheme.text, formatter: (v: number) => `${(v * 100).toFixed(1)}%` }, splitLine: { show: false } },
      ],
      series: [
        {
          name: '切换强度', type: 'line', smooth: true, symbol: 'none',
          data: timeline.slice(startCol).map(point => point.rotation),
          lineStyle: { color: '#f59e0b', width: 1.6 },
          areaStyle: { color: 'rgba(245,158,11,0.12)' },
        },
        {
          name: '全市场', type: 'line', smooth: true, symbol: 'none', yAxisIndex: 1,
          data: timeline.slice(startCol).map(point => point.market_pct),
          lineStyle: { color: 'rgba(128,140,160,0.55)', width: 1, type: 'dashed' },
        },
        ...(indexLine ? [{
          name: indexLine.label, type: 'line' as const, smooth: true, symbol: 'none', yAxisIndex: 1,
          data: indexLine.points.slice(startCol),
          lineStyle: { color: '#60a5fa', width: 1.4 },
          connectNulls: true,
        }] : []),
      ],
    }
  }, [data, dimLabel, indexLine, timeline, startCol, cols, phase, chartTheme])
  const chart = useEChart(chartOption)

  // 热力图: 行 = 热度降序板块 (综合分), 色 = 该桶板块涨幅 (红涨绿跌)
  const heatOption = useMemo<echarts.EChartsOption | null>(() => {
    const heatSeries = data?.series
    if (!heatSeries || !heatSeries.sectors.length || !heatSeries.buckets.length) return null
    const names = heatSeries.sectors.slice(0, HEAT_ROWS)
    const buckets = heatSeries.buckets.slice(startCol)
    const live = phase === 'live'
    const cells: { value: [number, number, number | null]; itemStyle?: { borderColor: string; borderWidth: number } }[] = []
    const absValues: number[] = []
    for (let row = 0; row < names.length; row++) {
      const values = heatSeries.matrix[row] ?? []
      for (let col = startCol; col < heatSeries.buckets.length; col++) {
        const value = values[col] ?? null
        cells.push({
          value: [col - startCol, row, value],
          // 盘中最后一列为未完成桶, 琥珀描边提示数据未封口
          itemStyle: live && col - startCol === cols - 1 ? { borderColor: '#f59e0b', borderWidth: 1.2 } : undefined,
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
      grid: { left: 86, right: 10, top: 8, bottom: 46 },
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
        axisLabel: { fontSize: 9, color: chartTheme.text, interval: Math.max(0, Math.ceil(buckets.length / 8) - 1) },
      },
      yAxis: {
        type: 'category', data: names, inverse: true,
        axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { fontSize: 10, color: chartTheme.textStrong, formatter: (value: string) => (value.length > 8 ? `${value.slice(0, 8)}…` : value) },
      },
      visualMap: {
        type: 'continuous', min: -maxAbs, max: maxAbs,
        orient: 'horizontal', left: 'center', bottom: 0,
        itemWidth: 8, itemHeight: 110, text: ['涨', '跌'],
        textStyle: { fontSize: 9, color: chartTheme.text },
        inRange: { color: ['#1E5C3C', '#3FA374', chartTheme.grid, '#D98B8B', '#C74040'] },
        calculable: false,
      },
      series: [{
        name: '热度热力图', type: 'heatmap',
        data: cells,
        itemStyle: { borderWidth: 1, borderColor: 'transparent' },
        emphasis: { itemStyle: { borderColor: '#fbbf24', borderWidth: 2 } },
      }],
    }
  }, [data, bucket, chartTheme, phase, startCol, cols])
  const heat = useEChart(heatOption, {
    onMouseOver: (params) => {
      if (params.seriesType !== 'heatmap') return
      const value = params.value as [number, number, number | null] | undefined
      const y = value?.[1]
      setHoverName(y != null ? heatNames[y] ?? null : null)
    },
    onMouseOut: () => setHoverName(null),
    onGlobalOut: () => setHoverName(null),
  })

  // 联动反向: 榜单行悬停 → 热力图整行琥珀描边 (downplay 上一行, highlight 当前行)
  const prevHoverRef = useRef<number>(-1)
  useEffect(() => {
    const inst = heat.instRef.current
    if (!inst || !data || data.status !== 'ok') return
    const rowIndices = (y: number) => Array.from({ length: cols }, (_, c) => y * cols + c)
    if (prevHoverRef.current >= 0) {
      inst.dispatchAction({ type: 'downplay', seriesIndex: 0, dataIndex: rowIndices(prevHoverRef.current) })
      prevHoverRef.current = -1
    }
    if (!hoverName) return
    const y = heatNames.indexOf(hoverName)
    if (y >= 0) {
      inst.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: rowIndices(y) })
      prevHoverRef.current = y
    }
  }, [hoverName, data, bucket, cols, heatNames, heat.instRef])

  // 强度图与热力图 x 轴指针跨实例联动 (窗口一致才可对齐)
  useEffect(() => {
    const a = chart.instRef.current
    const b = heat.instRef.current
    if (!a || !b) return
    a.group = CHART_CONNECT_GROUP
    b.group = CHART_CONNECT_GROUP
    echarts.connect(CHART_CONNECT_GROUP)
  }, [data, bucket, chart.instRef, heat.instRef])

  const onFlowChange = (value: string) => {
    setFlow(value)
    if (value) localStorage.setItem(`${FLOW_LS_PREFIX}${kind}`, value)
    else localStorage.removeItem(`${FLOW_LS_PREFIX}${kind}`)
  }

  const latest = data?.timeline?.[data.timeline.length - 1]
  const heatHeight = Math.max(180, heatRows * HEAT_ROW_HEIGHT + 62)

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
          {data?.status === 'ok' && latest ? `${data.date} ${data.as_of} · 切换强度 ${latest.rotation.toFixed(2)} · 领涨 ${latest.leader}` : '全量分钟聚合'}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-[9px] text-muted">指数</span>
          <select
            aria-label="指数叠加"
            className="h-6 max-w-24 truncate rounded border border-border bg-surface px-1 text-[10px] text-secondary outline-none focus:border-accent"
            value={indexSymbol}
            onChange={event => onIndexChange(event.target.value)}
          >
            {CORE_INDEXES.map(item => <option key={item.symbol} value={item.symbol}>{item.name}</option>)}
          </select>
          <span className="text-[9px] text-muted">资金流</span>
          <select
            aria-label="资金流扩展列"
            className="h-6 max-w-52 truncate rounded border border-border bg-surface px-1 text-[10px] text-secondary outline-none focus:border-accent"
            value={flow}
            onChange={event => onFlowChange(event.target.value)}
          >
            <option value="">不使用</option>
            {flowOptions.map(option => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select
            aria-label="分钟桶粒度"
            className="h-6 rounded border border-border bg-surface px-1 text-[10px] text-secondary outline-none focus:border-accent"
            value={bucket}
            onChange={event => setBucket(Number(event.target.value))}
          >
            {[1, 5, 15].map(value => <option key={value} value={value}>{value}分钟</option>)}
          </select>
          <RefreshCw className={`h-3 w-3 text-muted ${rotationQuery.isFetching ? 'animate-spin text-accent' : ''}`} />
        </div>
      </div>

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
          {heatRows > 0 && (
            <div className="rounded-lg border border-border/60 bg-elevated/30 p-1.5">
              <div className="px-1 pb-1 text-[9px] text-muted">
                热度板块 × 分钟轮动热力图 — 行按综合分{data.flow_available ? '+资金流' : ''}排序, 色为该桶板块涨幅 (红涨绿跌, 平淡近透明), 悬停与下方榜单联动
              </div>
              <div ref={heat.ref} style={{ height: heatHeight }} className="w-full" />
            </div>
          )}
          <div className="mt-2 grid grid-cols-1 gap-2 lg:grid-cols-[1.2fr_1fr]">
            <div className="rounded-lg border border-border/60 bg-elevated/30 p-1.5">
              <div className="px-1 pb-1 text-[9px] text-muted">
                切换强度 (1h 领涨梯队换血率) · 蓝线={indexLine ? indexLine.label : '指数'}(右轴) · 灰虚线=全市场(右轴) · 与热力图指针联动
              </div>
              <div ref={chart.ref} className="h-32 w-full" />
            </div>
            <div className="overflow-hidden rounded-lg border border-border/60">
              <div className="grid grid-cols-[minmax(0,1.4fr)_64px_64px_58px_minmax(72px,1fr)] border-b border-border bg-base/50 px-2 py-1.5 text-[9px] font-medium text-muted">
                <span>{dimLabel}</span><span className="text-right">现涨幅</span><span className="text-right">1h前</span><span className="text-right">排名变化</span><span className="text-right">资金流 / 综合分</span>
              </div>
              <div className="max-h-40 overflow-y-auto">
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
          <div className="mt-1.5 flex items-center gap-1.5 text-[9px] text-muted">
            <Database className="h-3 w-3" />
            {`${data.member_count} 个${dimLabel} · ${data.bucket_minutes}分钟桶 · 基准 ${data.basis === 'prev_close' ? '昨收' : data.basis === 'first_close' ? '今开' : '混合'}`}
            {data.flow_available ? ` · 资金流 ${data.flow_field}` : ' · 未启用资金流'}
            <span className="ml-auto">每 30s 自动刷新 · 排名变化 ↑切入 ↓退潮 (相对 1 小时前) · 热力图默认前 {HEAT_ROWS} 个热度板块</span>
          </div>
        </>
      )}
    </section>
  )
}
