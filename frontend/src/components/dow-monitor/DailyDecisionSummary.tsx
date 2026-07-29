import { cn } from '@/lib/cn'

import { formatServerTimestamp } from './formatServerTimestamp'
import type { DowDailyDecisionSummary as DailySummary } from './types'

const DIRECTION_CLASS: Record<DailySummary['direction'], string> = {
  BULLISH: 'dow-daily-summary--bullish',
  BEARISH: 'dow-daily-summary--bearish',
  RANGE: 'dow-daily-summary--range',
}

const DIRECTION_TEXT: Record<DailySummary['direction'], string> = {
  BULLISH: '走势偏强',
  BEARISH: '走势偏弱',
  RANGE: '方向不清楚',
}

const ACTION_TEXT: Record<DailySummary['action'], string> = {
  WATCH_BUY: '先观察，等待确认',
  HOLD: '可以继续观察持有',
  REDUCE_SELL: '注意控制风险，可考虑减仓',
  OBSERVE: '先观望，不追涨',
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function formatPrice(value: number): string {
  return value.toFixed(2)
}

function getCostValues(summary: DailySummary) {
  if (
    !isFiniteNumber(summary.current_price)
    || !isFiniteNumber(summary.vwap_price)
    || !isFiniteNumber(summary.vwap_distance_pct)
  ) {
    return null
  }
  return {
    currentPrice: summary.current_price,
    averageCost: summary.vwap_price,
    distancePct: summary.vwap_distance_pct,
  }
}

export function DailyDecisionSummary({
  summary,
}: {
  summary: DailySummary
}) {
  const updatedAt = formatServerTimestamp(summary.as_of_minute)
  const cost = getCostValues(summary)
  const costPosition = cost
    ? `${cost.distancePct >= 0 ? '高于' : '低于'}${Math.abs(cost.distancePct).toFixed(2)}%`
    : null
  const costMeaning = cost
    ? (
      cost.distancePct >= 0
        ? `今天市场的平均成交成本约为${formatPrice(cost.averageCost)}，当前价格为${formatPrice(cost.currentPrice)}。价格处于平均成本上方，短线相对偏强。`
        : `今天市场的平均成交成本约为${formatPrice(cost.averageCost)}，当前价格为${formatPrice(cost.currentPrice)}。价格处于平均成本下方，当天买入的资金整体承受一定压力，价格上方可能存在卖出压力。`
    )
    : '今天的成交金额或成交量还不完整，暂时不能计算可靠的平均成交成本。'

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
        <span>当前判断：<strong>{DIRECTION_TEXT[summary.direction]}</strong></span>
        <span>建议动作：<strong>{ACTION_TEXT[summary.action]}</strong></span>
      </div>

      {cost ? (
        <div className="dow-daily-summary__metrics">
          <span>今日平均成交成本：<strong>{formatPrice(cost.averageCost)}</strong></span>
          <span>当前价格：<strong>{formatPrice(cost.currentPrice)}</strong></span>
          <span>当前价相对成本：<strong>{costPosition}</strong></span>
        </div>
      ) : (
        <p className="dow-daily-summary__cost-unavailable">
          今日平均成交成本暂不可用
        </p>
      )}

      <p className="dow-daily-summary__conclusion">{summary.summary_text}</p>

      <p className="dow-daily-summary__confidence">
        证据一致度：<strong className="font-mono">{summary.confidence}%</strong>
        <span>表示当前多数指标的方向一致，不是上涨或下跌概率。</span>
      </p>

      <details className="dow-daily-summary__details">
        <summary>查看详细说明</summary>
        <div className="dow-daily-summary__details-body">
          <div>
            <strong>这说明什么</strong>
            <p>{costMeaning}</p>
          </div>

          <div>
            <strong>为什么这样判断</strong>
            {summary.phase_path.length > 0 && (
              <p>
                今日变化：{summary.phase_path.map(phase => phase.label).join(' → ')}
              </p>
            )}
            {summary.key_evidence.length > 0 ? (
              <ul>
                {summary.key_evidence.map(evidence => (
                  <li key={evidence.code}>{evidence.text}</li>
                ))}
              </ul>
            ) : (
              <p>今天暂时没有新的买卖信号，正在等待新的分钟数据。</p>
            )}
          </div>

          <div>
            <strong>什么情况下改变判断</strong>
            <p>{summary.reversal_condition}</p>
          </div>
        </div>
      </details>
    </section>
  )
}
