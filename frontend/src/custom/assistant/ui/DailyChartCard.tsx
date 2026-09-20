/**
 * 走势小卡 — 纯 SVG 零依赖, 支持日K蜡烛(daily_kline)与当日分时(intraday)。
 *
 * 数据来自工具流(tool_result.charts), 是工具真实返回的序列而非模型生成,
 * 符合本模块"结果可核对"铁律; 配色沿用 A 股口径: 涨红跌绿(仅用于价格/K线)。
 */
import { memo, type ReactNode } from 'react'
import type { AssistantChart } from '../client'

const W = 100
const H = 40

function fmt(v: number): string {
  return v >= 1000 ? v.toFixed(0) : v.toFixed(2)
}

function gradId(chart: AssistantChart): string {
  return `assistant-chart-${chart.kind}-${chart.symbol.replace(/[^a-zA-Z0-9]/g, '')}`
}

function titleOf(chart: AssistantChart): ReactNode {
  return (
    <>
      {chart.name || chart.symbol}
      {chart.name ? <span className="ml-1.5 font-mono text-muted">{chart.symbol}</span> : null}
      <span className="ml-1.5 text-muted">· {chart.kind === 'daily_kline' ? '日K' : '分时'}</span>
    </>
  )
}

function ChartShell({
  chart,
  meta,
  footerLeft,
  footerRight,
  ariaLabel,
  children,
}: {
  chart: AssistantChart
  meta: string
  footerLeft: ReactNode
  footerRight: ReactNode
  ariaLabel: string
  children: ReactNode
}) {
  const id = gradId(chart)
  return (
    <div className="overflow-hidden rounded-card border border-border bg-base/60">
      <div className="flex min-w-0 items-center justify-between gap-2 px-3 py-1.5 text-xs">
        <span className="truncate font-medium text-foreground">{titleOf(chart)}</span>
        <span className="shrink-0 font-mono text-muted">{meta}</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-32 w-full px-3"
        role="img"
        aria-label={ariaLabel}
      >
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={`hsl(var(--accent))`} stopOpacity="0.10" />
            <stop offset="100%" stopColor={`hsl(var(--accent))`} stopOpacity="0" />
          </linearGradient>
        </defs>
        {children}
      </svg>
      <div className="flex flex-wrap items-center justify-between gap-x-2 px-3 py-1.5 text-[11px]">
        <span className="font-mono text-muted">{footerLeft}</span>
        <span className="font-mono text-muted">{footerRight}</span>
      </div>
    </div>
  )
}

function KlineCard({ chart }: { chart: Extract<AssistantChart, { kind: 'daily_kline' }> }) {
  const points = chart.points
  const opens = points.map(p => p[1])
  const highs = points.map(p => p[2])
  const lows = points.map(p => p[3])
  const closes = points.map(p => p[4])
  const volumes = points.map(p => p[5])
  const min = Math.min(...lows)
  const max = Math.max(...highs)
  const span = max - min || 1
  const maxVol = Math.max(...volumes, 1)
  const n = points.length
  const px = (i: number) => (i / (n - 1)) * W
  // 价格带占上方约 78%, 底部留成交量带
  const py = (v: number) => H * 0.78 - ((v - min) / span) * H * 0.66
  const barW = Math.max((W / n) * 0.55, 0.5)
  const last = closes[n - 1]
  const chg = closes[0] > 0 ? (last - closes[0]) / closes[0] : 0
  const up = chg >= 0

  return (
    <ChartShell
      chart={chart}
      meta={`近 ${n} 日`}
      footerLeft={`低 ${fmt(min)} · 高 ${fmt(max)}`}
      footerRight={
        <>
          最新 <span className="text-foreground">{fmt(last)}</span>
          <span className={`ml-1.5 font-medium ${up ? 'text-bull' : 'text-bear'}`}>
            {up ? '+' : ''}{(chg * 100).toFixed(2)}%
          </span>
        </>
      }
      ariaLabel={`${chart.name || chart.symbol} 近 ${n} 个交易日日K`}
    >
      {volumes.map((v, i) => {
        const bullBar = closes[i] >= opens[i]
        const h = (v / maxVol) * H * 0.14
        return (
          <rect
            key={points[i][0]}
            x={(px(i) - barW / 2).toFixed(2)}
            y={(H - h).toFixed(2)}
            width={barW.toFixed(2)}
            height={h.toFixed(2)}
            fill={`hsl(var(--${bullBar ? 'bull' : 'bear'}))`}
            opacity="0.28"
          />
        )
      })}
      {points.map((p, i) => {
        const [, o, h, l, c] = p
        const bullBar = c >= o
        const color = `hsl(var(--${bullBar ? 'bull' : 'bear'}))`
        const bodyTop = py(Math.max(o, c))
        const bodyH = Math.max(Math.abs(py(o) - py(c)), 0.8)
        return (
          <g key={p[0]}>
            <line
              x1={px(i).toFixed(2)}
              x2={px(i).toFixed(2)}
              y1={py(h).toFixed(2)}
              y2={py(l).toFixed(2)}
              stroke={color}
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
            />
            <rect
              x={(px(i) - barW / 2).toFixed(2)}
              y={bodyTop.toFixed(2)}
              width={barW.toFixed(2)}
              height={bodyH.toFixed(2)}
              fill={color}
            />
          </g>
        )
      })}
    </ChartShell>
  )
}

function IntradayCard({ chart }: { chart: Extract<AssistantChart, { kind: 'intraday' }> }) {
  const points = chart.points
  const prices = points.map(p => p[1])
  const volumes = points.map(p => p[2])
  const baseline = chart.prev_close
  const min = Math.min(...prices, baseline ?? Infinity)
  const max = Math.max(...prices, baseline ?? -Infinity)
  const span = max - min || 1
  const maxVol = Math.max(...volumes, 1)
  const n = points.length
  const px = (i: number) => (i / (n - 1)) * W
  const py = (v: number) => H * 0.78 - ((v - min) / span) * H * 0.66
  const line = prices.map((v, i) => `${i === 0 ? 'M' : 'L'}${px(i).toFixed(2)},${py(v).toFixed(2)}`).join(' ')
  const area = `${line} L${W},${H} L0,${H} Z`
  const barW = Math.max((W / n) * 0.55, 0.5)
  const last = prices[n - 1]
  const ref = baseline ?? prices[0]
  const chg = ref > 0 ? (last - ref) / ref : 0
  const up = chg >= 0

  return (
    <ChartShell
      chart={chart}
      meta={`${points[0][0]} – ${points[n - 1][0]}`}
      footerLeft={`低 ${fmt(min)} · 高 ${fmt(max)}`}
      footerRight={
        <>
          最新 <span className="text-foreground">{fmt(last)}</span>
          <span className={`ml-1.5 font-medium ${up ? 'text-bull' : 'text-bear'}`}>
            {up ? '+' : ''}{(chg * 100).toFixed(2)}%
          </span>
        </>
      }
      ariaLabel={`${chart.name || chart.symbol} 当日分时走势`}
    >
      {baseline != null && (
        <line
          x1="0"
          x2={W}
          y1={py(baseline).toFixed(2)}
          y2={py(baseline).toFixed(2)}
          stroke="hsl(var(--fg-muted))"
          strokeWidth="1"
          strokeDasharray="3 2"
          vectorEffect="non-scaling-stroke"
          opacity="0.6"
        />
      )}
      {volumes.map((v, i) => {
        const refPrev = i > 0 ? prices[i - 1] : (baseline ?? prices[0])
        const h = (v / maxVol) * H * 0.14
        return (
          <rect
            key={points[i][0]}
            x={(px(i) - barW / 2).toFixed(2)}
            y={(H - h).toFixed(2)}
            width={barW.toFixed(2)}
            height={h.toFixed(2)}
            fill={`hsl(var(--${prices[i] >= refPrev ? 'bull' : 'bear'}))`}
            opacity="0.28"
          />
        )
      })}
      <path d={area} fill={`url(#${gradId(chart)})`} />
      <path
        d={line}
        fill="none"
        stroke={`hsl(var(--${up ? 'bull' : 'bear'}))`}
        strokeWidth="1.4"
        vectorEffect="non-scaling-stroke"
        strokeLinejoin="round"
      />
    </ChartShell>
  )
}

export const DailyChartCard = memo(function DailyChartCard({ chart }: { chart: AssistantChart }) {
  if (chart.points.length < 2) return null
  return chart.kind === 'daily_kline' ? <KlineCard chart={chart} /> : <IntradayCard chart={chart} />
})
