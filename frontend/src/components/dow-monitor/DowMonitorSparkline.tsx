import { cn } from '@/lib/cn'

function svgPoints(values: number[], width: number, height: number): string {
  if (values.length === 0) return ''
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const range = maximum - minimum || 1
  return values.map((value, index) => {
    const x = values.length === 1 ? width / 2 : index / (values.length - 1) * width
    const y = height - ((value - minimum) / range * (height - 2) + 1)
    return `${x.toFixed(2)},${y.toFixed(2)}`
  }).join(' ')
}

export function DowMonitorSparkline({
  symbol,
  values,
  className,
}: {
  symbol: string
  values: number[]
  className?: string
}) {
  const width = 104
  const height = 30
  if (values.length === 0) {
    return <span className={cn('text-xs text-muted', className)}>--</span>
  }
  const rising = values.at(-1)! >= values[0]
  return (
    <svg
      role="img"
      aria-label={`${symbol} 当日趋势`}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={cn('h-[30px] w-[104px] overflow-visible', className)}
    >
      <polyline
        data-testid="sparkline-line"
        points={svgPoints(values, width, height)}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
        className={rising ? 'text-danger' : 'text-emerald-400'}
      />
    </svg>
  )
}
