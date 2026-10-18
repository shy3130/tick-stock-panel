/**
 * 日内多盘扩展列的迷你图单元格 (集合竞价快照序列等)。
 *
 * 纯 SVG 实现, 不引第三方图表库: 点列归一化到单元格宽高,
 * line=折线 (价格轨迹), bar=柱状 (量/额等逐盘幅度)。
 * 单点/全空序列回退为文本占位, 不伪造趋势。
 */
import { useId } from 'react'

export type ExtSparkKind = 'line' | 'bar'

export function ExtSparkChart({ points, kind, width = 110, height = 36 }: {
  points: [string, number][]
  kind: ExtSparkKind
  width?: number
  height?: number
}) {
  const gradId = useId()
  const values = points.map(p => p[1]).filter(Number.isFinite)
  if (values.length < 2) {
    return <span className="text-[10px] text-muted tabular-nums">{values.length === 1 ? values[0] : '—'}</span>
  }

  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || Math.abs(max) || 1
  const pad = 2
  const h = height - pad * 2
  const x = (i: number) => (points.length === 1 ? 0 : (i / (points.length - 1)) * (width - pad * 2) + pad)
  const y = (v: number) => pad + h - ((v - min) / span) * h

  return (
    <svg width={width} height={height} className="block text-accent" aria-hidden>
      {kind === 'line' ? (
        <>
          {(min !== max) && (
            <>
              <defs>
                <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="currentColor" stopOpacity="0.25" />
                  <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
                </linearGradient>
              </defs>
              <polygon
                fill={`url(#${gradId})`}
                points={`${points.map((p, i) => `${x(i)},${y(p[1])}`).join(' ')} ${x(points.length - 1)},${height} ${x(0)},${height}`}
              />
            </>
          )}
          <polyline
            fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" strokeLinecap="round"
            points={points.map((p, i) => `${x(i)},${y(p[1])}`).join(' ')}
          />
          <circle cx={x(points.length - 1)} cy={y(points[points.length - 1][1])} r="1.6" fill="currentColor" />
        </>
      ) : (
        points.map((p, i) => {
          const barW = Math.max(1, (width - pad * 2) / points.length - 0.8)
          const top = min >= 0 ? y(p[1]) : Math.min(y(p[1]), y(0))
          const bottom = min >= 0 ? height - pad : Math.max(y(p[1]), y(0))
          return (
            <rect
              key={i} x={x(i) - barW / 2} y={top}
              width={barW} height={Math.max(1, bottom - top)}
              fill="currentColor" opacity={i === points.length - 1 ? 1 : 0.55} rx="0.5"
            />
          )
        })
      )}
    </svg>
  )
}
