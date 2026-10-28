/**
 * 日内多盘/日级扩展列的迷你图单元格 (集合竞价快照序列、近N日每日值等)。
 *
 * 纯 SVG 实现, 不引第三方图表库: 点列归一化到单元格宽高,
 * line=折线 (价格轨迹), bar=柱状 (量/额等逐盘/逐日幅度)。
 * 单点/全空/全等值序列回退为文本占位 — 全等值时柱状归一化分母退化,
 * 负值会渲染成满柱、正值塌成基线虚线, 都是伪趋势, 不如直接显示数值。
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
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 0
  // 全等值 (含单点/全空): 数值无变化可画, 回退文本
  if (values.length < 2 || min === max) {
    return <span className="text-[10px] text-muted tabular-nums">{values.length >= 1 ? values[0] : '—'}</span>
  }
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
