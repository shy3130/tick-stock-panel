import { cn } from '@/lib/cn'
import { MARKET_FILTER_OPTIONS, type MarketFilter } from '@/lib/market-display'


export function MarketFilterTabs({
  value,
  onChange,
  includeAll = true,
  className,
}: {
  value: MarketFilter
  onChange: (value: MarketFilter) => void
  includeAll?: boolean
  className?: string
}) {
  const options = includeAll
    ? MARKET_FILTER_OPTIONS
    : MARKET_FILTER_OPTIONS.filter(option => option.value !== 'all')

  return (
    <div className={cn('flex h-8 items-center overflow-hidden rounded-btn border border-border bg-surface', className)}>
      {options.map(option => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={cn(
            'h-full px-2.5 text-xs font-medium transition-colors cursor-pointer',
            value === option.value
              ? 'bg-accent/15 text-accent'
              : 'text-muted hover:bg-elevated hover:text-secondary',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
