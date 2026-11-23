import { Clock } from 'lucide-react'
import type { QuoteFreshness } from '@/lib/api'
import { advanceQuoteFreshness, quoteFreshnessLabel } from '@/lib/quote-freshness'
import { useQuoteClock } from '@/lib/useQuoteClock'

export function QuoteFreshnessBadge({ freshness, compact = false }: { freshness?: QuoteFreshness | null; compact?: boolean }) {
  const now = useQuoteClock()
  // The original check time survives persisted queries and window reactivation.
  const current = freshness ? advanceQuoteFreshness(freshness, Math.max(0, now - freshness.checked_at)) : null
  if (!current || (compact && current.state === 'live')) return null
  const warning = ['stale', 'delayed', 'missing', 'unknown'].includes(current.state)
  const title = `${current.message}${current.data_date ? `；数据日期 ${current.data_date}` : ''}`
  return (
    <span title={title} aria-label={title}
      className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-[11px] leading-5 ${warning ? 'text-warning' : 'text-muted'}`}>
      <Clock className="h-3 w-3" aria-hidden="true" />
      {quoteFreshnessLabel(current)}
    </span>
  )
}
