import { cn } from '@/lib/cn'

import { formatServerTimestamp } from './formatServerTimestamp'
import type { DowDailyDecisionSummary as DailySummary } from './types'

const DIRECTION_CLASS: Record<DailySummary['direction'], string> = {
  BULLISH: 'dow-daily-summary--bullish',
  BEARISH: 'dow-daily-summary--bearish',
  RANGE: 'dow-daily-summary--range',
}

export function DailyDecisionSummary({
  summary,
}: {
  summary: DailySummary
}) {
  const updatedAt = formatServerTimestamp(summary.as_of_minute)

  return (
    <section
      data-testid="daily-decision-summary"
      aria-label="今日综合决策"
      className={cn('dow-daily-summary', DIRECTION_CLASS[summary.direction])}
    >
      <div className="dow-daily-summary__head">
        <span className="font-semibold text-foreground">今日综合决策</span>
        <span className="font-mono text-[9px] text-muted">
          {summary.status_label}{updatedAt ? ` · 更新 ${updatedAt}` : ''}
        </span>
      </div>

      <div className="dow-daily-summary__decision">
        <strong>{summary.direction_label} · {summary.action_label}</strong>
        <span className="font-mono">{summary.confidence}%</span>
      </div>

      <p className="dow-daily-summary__conclusion">{summary.summary_text}</p>

      {summary.phase_path.length > 0 && (
        <div className="dow-daily-summary__line">
          <span>阶段：</span>
          <strong>{summary.phase_path.map(phase => phase.label).join(' → ')}</strong>
        </div>
      )}

      {summary.key_evidence.length > 0 && (
        <div className="dow-daily-summary__line">
          <span>主因：</span>
          <span>{summary.key_evidence.map(evidence => evidence.text).join(' ｜ ')}</span>
        </div>
      )}

      <div className="dow-daily-summary__line dow-daily-summary__reversal">
        <span>转向条件：</span>
        <span>{summary.reversal_condition}</span>
      </div>
    </section>
  )
}
