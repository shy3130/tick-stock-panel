/**
 * 日内多盘/日级扩展列的迷你图单元格 (集合竞价快照序列、近N日每日值等)。
 *
 * 纯 SVG 实现, 不引第三方图表库: 点列归一化到单元格宽高,
 * line=折线 (价格轨迹), bar=柱状 (量/额等逐盘/逐日幅度)。
 * 单点/全空/全等值序列回退为文本占位, 不画伪趋势。
 * 柱状图从零起算, 折线图按自身值域缩放。
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
  const finitePoints = points.filter(p => Number.isFinite(p[1]))
  const values = finitePoints.map(p => p[1])
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 0
  // 全等值 (含单点/全空): 数值无变化可画, 回退文本
  if (values.length < 2 || min === max) {
    return <span className="text-[10px] text-muted tabular-nums">{values.length >= 1 ? values[0] : '—'}</span>
  }
  // 柱体表达绝对幅度; 只按 min/max 缩放会压扁较小正值, 并把全负序列的零轴移出图外。
  const domainMin = kind === 'bar' ? Math.min(0, min) : min
  const domainMax = kind === 'bar' ? Math.max(0, max) : max
  const span = domainMax - domainMin
  const pad = 2
  const h = height - pad * 2
  const x = (i: number) => (i / (finitePoints.length - 1)) * (width - pad * 2) + pad
  const y = (v: number) => pad + h - ((v - domainMin) / span) * h

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
                points={`${finitePoints.map((p, i) => `${x(i)},${y(p[1])}`).join(' ')} ${x(finitePoints.length - 1)},${height} ${x(0)},${height}`}
              />
            </>
          )}
          <polyline
            fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" strokeLinecap="round"
            points={finitePoints.map((p, i) => `${x(i)},${y(p[1])}`).join(' ')}
          />
          <circle cx={x(finitePoints.length - 1)} cy={y(values[values.length - 1])} r="1.6" fill="currentColor" />
        </>
      ) : (
        finitePoints.map((p, i) => {
          const step = (width - pad * 2) / finitePoints.length
          const barW = Math.min(step, Math.max(0.5, step - 0.8))
          const top = Math.min(y(p[1]), y(0))
          const bottom = Math.max(y(p[1]), y(0))
          return (
            <rect
              key={i} x={pad + i * step + (step - barW) / 2} y={top}
              width={barW} height={bottom - top}
              fill="currentColor" opacity={i === finitePoints.length - 1 ? 1 : 0.55} rx="0.5"
            />
          )
        })
      )}
    </svg>
  )
}
