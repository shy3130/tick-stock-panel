/**
 * 市场环境(Regime)页 — 每日环境状态时序趋势 + 状态分布。
 *
 * 数据来源: 后端 regime_builder 批算的时序表(每日离散状态 + 多维指标)。
 * 聚焦历史趋势与状态分布。深色工作台风格, 对齐仓库现有卡片视觉。
 */
import { useMemo, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { Activity, RefreshCw, Gauge, TrendingUp, TrendingDown, Minus } from 'lucide-react'
import {
  type RegimeRow, type RegimeState,
  REGIME_STATE_LABELS, REGIME_STATE_COLORS, RQK, regimeApi,
} from '@/lib/regime'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

const AXIS = '#a1a1aa'
const GRID = 'rgba(160,160,170,0.18)'
const TEXT_STRONG = '#e4e4e7'
const TOOLTIP_BG = 'rgba(24,24,27,0.92)'
const TOOLTIP_BORDER = 'rgba(82,82,91,0.6)'

const STATE_ORDER: RegimeState[] = ['strong', 'lean_strong', 'range', 'lean_weak', 'weak']

type RangePreset = '1y' | '2y' | 'all'

const RANGE_LABELS: Record<RangePreset, string> = { '1y': '1年', '2y': '2年', all: '全部' }

function resolveLimit(preset: RangePreset): number | undefined {
  if (preset === '1y') return 250
  if (preset === '2y') return 500
  return undefined
}

function resolveDays(preset: RangePreset, rows: number): number {
  if (preset === '1y') return 250
  if (preset === '2y') return 500
  return rows > 0 ? Math.min(rows, 1000) : 1000
}

function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="panel-header !border-0 !px-0 !py-0">
      <div className="flex items-center gap-2">
        <Icon className="h-3.5 w-3.5 text-accent" />
        <h2 className="section-title text-xs">{title}</h2>
      </div>
      {hint != null && <span className="text-[10px] font-mono text-muted">{hint}</span>}
    </div>
  )
}

const cardCls = 'panel'

export function Regime() {
  const qc = useQueryClient()
  const [range, setRange] = useState<RangePreset>('1y')
  const [recomputing, setRecomputing] = useState(false)

  const coverage = useQuery({
    queryKey: RQK.coverage,
    queryFn: () => regimeApi.coverage(),
    staleTime: 5 * 60 * 1000,
  })

  const limit = resolveLimit(range)
  const histRange = range === 'all'
    ? { start: coverage.data?.earliest_date ?? undefined, end: coverage.data?.latest_date ?? undefined }
    : { start: undefined, end: undefined }

  const history = useQuery({
    queryKey: RQK.history(range),
    queryFn: () => regimeApi.history(histRange.start, histRange.end, limit),
    staleTime: 5 * 60 * 1000,
  })

  const days = resolveDays(range, history.data?.total ?? 0)
  const states = useQuery({
    queryKey: RQK.states(days),
    queryFn: () => regimeApi.states(days),
    staleTime: 5 * 60 * 1000,
  })

  const rows: RegimeRow[] = history.data?.rows ?? []
  const latest = rows.length > 0 ? rows[rows.length - 1] : null

  const handleRecompute = async () => {
    setRecomputing(true)
    try {
      const r = await regimeApi.recompute()
      toast(`重算完成,${r.computed} 天`, 'success')
      qc.invalidateQueries({ queryKey: ['regime-history'] })
      qc.invalidateQueries({ queryKey: ['regime-states'] })
      qc.invalidateQueries({ queryKey: RQK.coverage })
    } catch (e) {
      toast((e as Error).message || '重算失败', 'error')
    } finally {
      setRecomputing(false)
    }
  }

  // ── 当前势头 ──
  const momentum = useMemo(() => {
    if (rows.length === 0) return null
    const lastState = rows[rows.length - 1].state
    let streak = 1
    for (let i = rows.length - 2; i >= 0; i--) {
      if (rows[i].state === lastState) streak++
      else break
    }
    const recent = rows.slice(-5)
    const slope = recent.length >= 2
      ? (recent[recent.length - 1].score - recent[0].score) / (recent.length - 1)
      : 0
    return { streak, state: lastState, slope }
  }, [rows])

  // ── 趋势图 ──
  const trendOption = useMemo<EChartsOption | null>(() => {
    if (rows.length === 0) return null
    const dates = rows.map(r => r.date)
    const scores = rows.map(r => r.score)
    const limitUps = rows.map(r => r.limit_up)

    const stateBands: [{ xAxis: string; itemStyle: { color: string; opacity: number } }, { xAxis: string }][] = []
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

    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: TOOLTIP_BG, borderColor: TOOLTIP_BORDER, textStyle: { color: AXIS } },
      legend: {
        data: ['综合分', '涨停数'],
        textStyle: { color: AXIS, fontSize: 10 }, top: 0,
      },
      grid: { left: 48, right: 64, top: 36, bottom: 56 },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLabel: { color: AXIS, fontSize: 10, formatter: (v: string) => v.slice(5) },
        axisLine: { lineStyle: { color: GRID } },
      },
      yAxis: [
        { type: 'value', name: '涨停', position: 'left', axisLabel: { color: AXIS, fontSize: 10 }, splitLine: { show: false }, nameTextStyle: { color: AXIS } },
        { type: 'value', name: '综合分', min: 0, max: 100, position: 'right', axisLabel: { color: AXIS, fontSize: 10 }, splitLine: { lineStyle: { color: GRID } }, nameTextStyle: { color: AXIS } },
      ],
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - (60 / days) * 100) },
        { type: 'slider', bottom: 8, height: 16, borderColor: AXIS, fillerColor: 'rgba(59,130,246,0.12)', textStyle: { color: AXIS } },
      ],
      series: [
        { name: '涨停数', type: 'bar', data: limitUps, yAxisIndex: 0, barMaxWidth: 6,
          itemStyle: { color: REGIME_STATE_COLORS.strong, opacity: 0.35 }, z: 1 },
        { name: '综合分', type: 'line', data: scores, smooth: true, symbol: 'none', yAxisIndex: 1,
          lineStyle: { width: 1.5, color: TEXT_STRONG }, areaStyle: { opacity: 0.06 }, z: 3,
          markArea: { silent: true, data: stateBands },
          markLine: {
            silent: true, symbol: 'none',
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
  }, [rows, days])
  const trendRef = useECharts(trendOption, [trendOption])

  // ── 分布饼图 ──
  const pieOption = useMemo<EChartsOption | null>(() => {
    const dist = states.data?.distribution ?? []
    if (dist.length === 0) return null
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'item', backgroundColor: TOOLTIP_BG, borderColor: TOOLTIP_BORDER, textStyle: { color: AXIS } },
      series: [{
        type: 'pie', radius: ['42%', '65%'], center: ['50%', '52%'],
        label: { position: 'outside', color: AXIS, fontSize: 10, formatter: '{b}  {d}%' },
        labelLine: { show: true, lineStyle: { color: GRID } },
        itemStyle: { borderColor: 'rgba(24,24,27,0.6)', borderWidth: 2 },
        data: dist.map(d => ({
          name: d.label, value: d.count,
          itemStyle: { color: REGIME_STATE_COLORS[d.state as RegimeState] ?? AXIS },
        })),
      }],
    }
  }, [states.data])
  const pieRef = useECharts(pieOption, [pieOption])

  return (
    <div className="workspace-page">
      <div className="workspace-content mx-auto max-w-[1440px] gap-3">
        <div className="workspace-toolbar justify-between">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Activity className="h-4 w-4 text-accent" />
            <h1 className="section-title text-base">市场环境</h1>
            {coverage.data && coverage.data.rows > 0 && (
              <span className="text-[10px] font-mono text-muted">
                {coverage.data.earliest_date} ~ {coverage.data.latest_date} · {coverage.data.rows} 天
              </span>
            )}
          </div>
          <div className="workspace-toolbar">
            <div className="flex overflow-hidden rounded-btn border border-border">
              {(['1y', '2y', 'all'] as const).map(k => (
                <button key={k} onClick={() => setRange(k)}
                  className={cn('h-8 px-3 text-xs transition-colors',
                    range === k ? 'bg-accent text-white' : 'bg-surface text-secondary hover:text-accent')}>
                  {RANGE_LABELS[k]}
                </button>
              ))}
            </div>
            <button onClick={handleRecompute} disabled={recomputing}
              className="btn-secondary text-xs">
              <RefreshCw className={cn('h-3 w-3', recomputing && 'animate-spin')} />
              {recomputing ? '重算中…' : '重算'}
            </button>
          </div>
        </div>

        {latest ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <div className={cn(cardCls)}>
              <div className="panel-body">
                <div className="section-kicker flex items-center gap-1.5">
                  <Gauge className="h-3 w-3" /> 最新状态 · {latest.date}
                </div>
                <div className="mt-1.5 flex items-baseline gap-2">
                  <span className="metric-value text-2xl" style={{ color: REGIME_STATE_COLORS[latest.state] }}>
                    {REGIME_STATE_LABELS[latest.state]}
                  </span>
                  <span className="text-sm text-muted">{latest.score} 分</span>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-base">
                  <div className="h-full rounded-full transition-all"
                    style={{ width: `${Math.max(2, Math.min(100, latest.score))}%`, backgroundColor: REGIME_STATE_COLORS[latest.state] }} />
                </div>
              </div>
            </div>

            <div className={cn(cardCls)}>
              <div className="panel-body">
                <div className="section-kicker flex items-center gap-1.5">
                  {(() => {
                    const TrendIcon = (momentum?.slope ?? 0) > 0.5 ? TrendingUp : (momentum?.slope ?? 0) < -0.5 ? TrendingDown : Minus
                    return <TrendIcon className={cn('h-3 w-3', (momentum?.slope ?? 0) > 0.5 ? 'text-bull' : (momentum?.slope ?? 0) < -0.5 ? 'text-bear' : 'text-muted')} />
                  })()} 当前势头
                </div>
                {momentum ? (
                  <>
                    <div className="mt-1.5 text-sm font-semibold text-foreground">
                      连续 <span style={{ color: REGIME_STATE_COLORS[momentum.state] }}>{momentum.streak}</span> 天{REGIME_STATE_LABELS[momentum.state]}
                    </div>
                    <div className="mt-1 text-[10px] text-muted">
                      5日{(momentum.slope > 0 ? '改善' : momentum.slope < 0 ? '恶化' : '持平')}
                    </div>
                  </>
                ) : <div className="mt-1.5 text-sm text-muted">—</div>}
              </div>
            </div>

            <div className={cn(cardCls, 'col-span-2 sm:col-span-1')}>
              <div className="panel-body">
                <div className="section-kicker flex items-center gap-1.5">
                  <Activity className="h-3 w-3" /> 四维拆解
                </div>
                <div className="mt-2 space-y-1">
                  {([
                    { label: '赚钱', val: latest.profit_score, color: 'hsl(var(--warning))' },
                    { label: '投机', val: latest.speculation_score, color: 'hsl(var(--accent))' },
                    { label: '抗跌', val: latest.resilience_score, color: 'hsl(var(--bear))' },
                    { label: '趋势', val: latest.trend_score, color: 'hsl(var(--accent))' },
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
            </div>
          </div>
        ) : (
          <div className="panel border-dashed p-8 text-center text-sm text-muted">
            {history.isLoading ? '加载中…' : '暂无环境数据，请先运行盘后管道或点击「重算」'}
          </div>
        )}

        {rows.length > 0 && (
          <div className={cn(cardCls)}>
            <div className="panel-body space-y-2.5">
              <SectionTitle icon={Activity} title="状态时间轴"
                hint={`${rows[0]?.date} → ${rows[rows.length - 1]?.date} · ${rows.length} 天`} />
              <div className="flex h-7 w-full overflow-hidden rounded-btn">
                {rows.map(r => (
                  <div key={r.date} title={`${r.date} ${REGIME_STATE_LABELS[r.state]}(${r.score})`}
                    className="min-w-[2px] flex-1 transition-opacity hover:opacity-80"
                    style={{ backgroundColor: REGIME_STATE_COLORS[r.state] }} />
                ))}
              </div>
              <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-[10px] text-muted">
                {STATE_ORDER.map(s => (
                  <span key={s} className="flex items-center gap-1">
                    <span className="status-dot" style={{ backgroundColor: REGIME_STATE_COLORS[s] }} />
                    {REGIME_STATE_LABELS[s]}
                  </span>
                ))}
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <div className={cn(cardCls, 'lg:col-span-2')}>
            <div className="panel-body">
              <SectionTitle icon={Activity} title="环境综合分趋势"
                hint="综合分主线 · 背景色=状态 · 阈值虚线" />
              <div ref={trendRef} className="mt-2 h-[320px]" />
            </div>
          </div>
          <div className={cn(cardCls)}>
            <div className="panel-body">
              <SectionTitle icon={Gauge} title="状态分布" hint={`近 ${days} 天`} />
              <div ref={pieRef} className="mt-2 h-[320px]" />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
