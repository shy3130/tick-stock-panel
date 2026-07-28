import { cn } from '@/lib/cn'

import { formatServerTimestamp } from './formatServerTimestamp'
import type { DowMinuteDecision, DowTimeframe } from './types'

const TIMEFRAME_LABELS: Record<DowTimeframe, string> = {
  '5m': '5分钟',
  '15m': '15分钟',
  '30m': '30分钟',
  '60m': '60分钟',
  day: '日线',
}

function decisionTone(decision: DowMinuteDecision | null) {
  if (!decision || decision.action === 'OBSERVE') {
    return 'border-amber-500/25 bg-amber-500/5'
  }
  if (decision.direction === 'BULLISH') {
    return 'border-emerald-500/25 bg-emerald-500/5'
  }
  if (decision.direction === 'BEARISH') {
    return 'border-red-500/25 bg-red-500/5'
  }
  return 'border-border bg-elevated/30'
}

function directionTone(decision: DowMinuteDecision | null) {
  if (!decision) return 'text-amber-300'
  if (decision.direction === 'BULLISH') return 'text-emerald-300'
  if (decision.direction === 'BEARISH') return 'text-red-300'
  return 'text-amber-300'
}

function evidenceItems(items: string[], fallback: string) {
  if (items.length === 0) {
    return <li className="text-muted">{fallback}</li>
  }
  return items.map(item => <li key={item}>{item}</li>)
}

export function MinuteDecisionPanel({
  decision,
}: {
  decision: DowMinuteDecision | null
}) {
  const decisionTime = decision
    ? formatServerTimestamp(decision.decision_minute)
    : null
  const dominant = decision?.dominant_timeframe
    ? TIMEFRAME_LABELS[decision.dominant_timeframe]
    : null

  return (
    <section
      data-testid="minute-decision-panel"
      aria-label="分钟决策分析中心"
      className={cn(
        'min-w-0 rounded border px-2.5 py-2 text-[10px]',
        decisionTone(decision),
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <div className="min-w-0">
          <div className="text-[9px] font-medium tracking-wide text-muted">
            分钟决策分析中心
          </div>
          <div className="mt-0.5 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <strong className={cn('text-sm', directionTone(decision))}>
              {decision?.direction_label ?? '等待分钟决策'}
            </strong>
            <span className="font-semibold text-foreground">
              {decision?.action_label ?? '继续观察'}
            </span>
            {decision && (
              <span className="font-mono text-xs tabular-nums text-secondary">
                {decision.confidence}%
              </span>
            )}
          </div>
        </div>
        <div className="min-w-0 text-right text-[9px] text-muted">
          <div>{decision?.status_label ?? '等待首个完整分钟'}</div>
          {decisionTime && <div className="font-mono">决策 {decisionTime}</div>}
        </div>
      </div>

      <div className="mt-1.5 flex min-w-0 flex-wrap gap-1 text-[9px]">
        {dominant && (
          <span className="rounded bg-accent/10 px-1.5 py-0.5 text-accent">
            {dominant}主导
          </span>
        )}
        {decision?.confirmation_timeframes.map(timeframe => (
          <span
            key={timeframe}
            className="rounded bg-elevated px-1.5 py-0.5 text-secondary"
          >
            {TIMEFRAME_LABELS[timeframe]}确认
          </span>
        ))}
      </div>

      {decision?.risk_warning && (
        <div className="mt-1.5 rounded border border-red-500/30 bg-red-500/10 px-2 py-1.5">
          <div className="font-semibold text-red-300">{decision.risk_warning.title}</div>
          <div className="mt-0.5 break-words text-secondary">
            {decision.risk_warning.message}
          </div>
        </div>
      )}

      {decision?.summary_text && (
        <p className="mt-1.5 break-words text-secondary">{decision.summary_text}</p>
      )}

      {(decision?.key_drivers?.length ?? 0) > 0 && (
        <ul className="mt-1.5 flex min-w-0 flex-wrap gap-1">
          {decision?.key_drivers?.slice(0, 3).map(driver => (
            <li
              key={driver.driver_code}
              className={cn(
                'rounded bg-elevated px-1.5 py-0.5 text-[9px]',
                driver.direction === 'BEARISH'
                  ? 'text-red-300'
                  : driver.direction === 'BULLISH'
                    ? 'text-emerald-300'
                    : 'text-secondary',
              )}
            >
              {driver.text}
            </li>
          ))}
        </ul>
      )}

      {(decision?.turn_stronger_condition || decision?.turn_weaker_condition) && (
        <div className="mt-1.5 grid min-w-0 grid-cols-1 gap-0.5 text-[9px]">
          {decision.turn_stronger_condition && (
            <div className="break-words text-secondary">
              <span className="font-medium text-emerald-300">转强：</span>
              {decision.turn_stronger_condition}
            </div>
          )}
          {decision.turn_weaker_condition && (
            <div className="break-words text-secondary">
              <span className="font-medium text-red-300">转弱：</span>
              {decision.turn_weaker_condition}
            </div>
          )}
        </div>
      )}

      <details className="mt-1.5 min-w-0 border-t border-border/60 pt-1.5">
        <summary className="cursor-pointer text-[9px] text-muted">展开分析依据</summary>
        <div
          data-testid="minute-decision-evidence"
          className="mt-1.5 grid min-w-0 grid-cols-1 gap-2 break-words sm:grid-cols-2"
        >
          <div className="min-w-0">
            <div className="font-medium text-emerald-300">支持理由</div>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-3.5 text-secondary">
              {evidenceItems(
                decision?.supporting_reasons ?? [],
                '等待分钟结构与资金数据确认',
              )}
            </ul>
          </div>
          <div className="min-w-0">
            <div className="font-medium text-amber-300">反向风险</div>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-3.5 text-secondary">
              {evidenceItems(
                decision?.contrary_risks ?? [],
                '暂未识别额外反向风险',
              )}
            </ul>
          </div>
        </div>

        <div className="mt-2 min-w-0 text-[9px]">
          <span className="font-medium text-red-300">失效条件：</span>
          <span className="break-words text-secondary">
            {decision?.invalidation_conditions.join('；') || '尚未形成可执行失效条件'}
          </span>
        </div>
      </details>
    </section>
  )
}
