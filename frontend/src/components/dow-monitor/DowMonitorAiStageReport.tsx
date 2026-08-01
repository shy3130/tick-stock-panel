import type { ReactNode } from 'react'

import { formatServerTimestamp } from './formatServerTimestamp'
import type { DowMonitorHalfHourAiAnalysis } from './types'


const DIRECTION_LABELS = {
  UP: '上升通道',
  DOWN: '下降通道',
  RANGE: '横盘区间',
  TRANSITION: '趋势转换',
} as const

const CHANGE_LABELS = {
  STRENGTHENING: '机会增强',
  WEAKENING: '机会减弱',
  UNCHANGED: '变化有限',
  REVERSING: '方向反转',
} as const

const CONFIDENCE_LABELS = {
  HIGH: '高',
  MEDIUM: '中',
  LOW: '低',
} as const

function hasText(value: string | null | undefined) {
  return Boolean(value?.trim())
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h4 className="font-medium">{title}</h4>
      <div className="mt-3 space-y-4 leading-6 text-secondary">{children}</div>
    </section>
  )
}

function Subsection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h5 className="text-xs font-medium text-muted">{title}</h5>
      <div className="mt-1.5">{children}</div>
    </div>
  )
}

function List({ items }: { items: string[] }) {
  const visibleItems = items.filter(hasText)
  if (visibleItems.length === 0) return null
  return (
    <ul className="list-disc space-y-1.5 pl-5">
      {visibleItems.map((item, index) => (
        <li key={`${item}-${index}`} className="break-words">{item}</li>
      ))}
    </ul>
  )
}

function AdviceCard({ title, advice }: { title: string; advice: string }) {
  if (!hasText(advice)) return null
  return (
    <section className="rounded-card border border-border p-3">
      <h4 className="text-xs font-medium text-muted">{title}</h4>
      <p className="mt-1 break-words leading-6 text-secondary">{advice}</p>
    </section>
  )
}

function ConditionCard({
  title,
  items,
  tone,
}: {
  title: string
  items: string[]
  tone: 'accent' | 'warning' | 'neutral'
}) {
  if (!items.some(hasText)) return null
  const toneClass = tone === 'accent'
    ? 'border-accent/40 bg-accent/5'
    : tone === 'warning'
      ? 'border-warning/40 bg-warning/5'
      : 'border-border bg-elevated'
  return (
    <section className={`rounded-card border p-3 ${toneClass}`}>
      <h4 className="font-medium">{title}</h4>
      <div className="mt-2 text-secondary"><List items={items} /></div>
    </section>
  )
}

export function DowMonitorAiStageReport({
  analysis,
}: {
  analysis: DowMonitorHalfHourAiAnalysis
}) {
  const report = analysis.report
  if (!report) return null

  const start = formatServerTimestamp(analysis.stage_start)
  const cutoff = formatServerTimestamp(analysis.data_cutoff)
  const hasAdvice = hasText(report.holding_advice.advice)
    || hasText(report.watching_advice.advice)
  const hasNextStageConditions = [
    ...report.next_stage_conditions.strengthen,
    ...report.next_stage_conditions.risk,
    ...report.next_stage_conditions.invalidation,
  ].some(hasText)
  const visiblePatterns = report.patterns.filter(
    (pattern) => hasText(pattern.name) || hasText(pattern.explanation),
  )
  const hasStageEvidence = report.stage_path.length > 0
    || report.hidden_changes.some(hasText)
    || hasText(report.comparison_with_previous)
  const hasDayEvidence = hasText(report.day_overview)
    || hasText(report.channel.explanation)
    || visiblePatterns.length > 0
    || hasText(report.volume_capital_interpretation)
  const hasQualityEvidence = hasText(report.confidence)
    || analysis.data_quality.some(hasText)

  return (
    <div className="space-y-5 text-sm">
      <section
        aria-label="阶段结论"
        className="rounded-card border border-border bg-elevated p-4"
      >
        <div className="text-xs font-medium text-muted">结论</div>
        <h3 className="mt-1 break-words text-lg font-semibold">
          {report.headline.title}
        </h3>
        {hasText(report.headline.summary) && (
          <p className="mt-2 break-words leading-6 text-secondary">
            {report.headline.summary}
          </p>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted">
          <span className="rounded border border-border px-2 py-0.5">
            {CHANGE_LABELS[report.headline.opportunity_change]}
          </span>
          <span>
            北京时间 {start?.slice(11) ?? '--'} 至 {cutoff?.slice(11) ?? '--'}
          </span>
          {analysis.stage_trading_minutes != null && (
            <span>{analysis.stage_trading_minutes} 个交易分钟</span>
          )}
        </div>
      </section>

      {hasAdvice && (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <AdviceCard title="持仓者建议" advice={report.holding_advice.advice} />
          <AdviceCard title="未参与者建议" advice={report.watching_advice.advice} />
        </div>
      )}

      {hasNextStageConditions && (
        <section aria-labelledby="next-stage-title">
          <h3 id="next-stage-title" className="font-medium">下一阶段只盯三件事</h3>
          <div
            data-testid="next-stage-conditions"
            className="mt-2 grid grid-cols-1 gap-2"
          >
            <ConditionCard
              title="增强确认"
              items={report.next_stage_conditions.strengthen}
              tone="accent"
            />
            <ConditionCard
              title="风险出现"
              items={report.next_stage_conditions.risk}
              tone="warning"
            />
            <ConditionCard
              title="判断失效"
              items={report.next_stage_conditions.invalidation}
              tone="neutral"
            />
          </div>
        </section>
      )}

      <details className="group rounded-card border border-border">
        <summary className="cursor-pointer list-none p-3 font-medium">
          <span className="group-open:hidden">
            展开完整分析（分钟路径、形态、量价、数据质量）
          </span>
          <span className="hidden group-open:inline">收起完整分析</span>
        </summary>
        <div className="space-y-5 border-t border-border p-4">
          {hasStageEvidence && (
            <Section title="本小时发生了什么">
              {report.stage_path.length > 0 && (
                <Subsection title="分钟路径">
                  <div className="space-y-2">
                    {report.stage_path.map((item, index) => (
                      <div
                        key={`${item.period}-${index}`}
                        className="rounded-card bg-elevated p-3"
                      >
                        <strong>{item.period}</strong>
                        <p className="break-words">{item.description}</p>
                      </div>
                    ))}
                  </div>
                </Subsection>
              )}
              {report.hidden_changes.some(hasText) && (
                <Subsection title="分钟K线隐藏变化">
                  <List items={report.hidden_changes} />
                </Subsection>
              )}
              {hasText(report.comparison_with_previous) && (
                <Subsection title="与上一阶段相比">
                  <p className="break-words">{report.comparison_with_previous}</p>
                </Subsection>
              )}
            </Section>
          )}

          {hasDayEvidence && (
            <Section title="当日整体结构与量价资金">
              {hasText(report.day_overview) && (
                <Subsection title="当日截至当前">
                  <p className="break-words">{report.day_overview}</p>
                </Subsection>
              )}
              {(hasText(report.channel.explanation) || visiblePatterns.length > 0) && (
                <Subsection title="通道与形态">
                  {hasText(report.channel.explanation) && (
                    <div className="rounded-card border border-border p-3">
                      <strong>{DIRECTION_LABELS[report.channel.direction]}</strong>
                      <p className="break-words">{report.channel.explanation}</p>
                    </div>
                  )}
                  {visiblePatterns.length > 0 && (
                    <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                      {visiblePatterns.map((pattern, index) => (
                        <div
                          key={`${pattern.name}-${index}`}
                          className="rounded-card border border-border p-3"
                        >
                          {hasText(pattern.name) && <strong>{pattern.name}</strong>}
                          {hasText(pattern.explanation) && (
                            <p className="break-words">{pattern.explanation}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </Subsection>
              )}
              {hasText(report.volume_capital_interpretation) && (
                <Subsection title="量价与资金含义">
                  <p className="break-words">{report.volume_capital_interpretation}</p>
                </Subsection>
              )}
            </Section>
          )}

          {hasQualityEvidence && (
            <Section title="分析依据与数据质量">
              <p>置信度：{CONFIDENCE_LABELS[report.confidence]}</p>
              <List items={analysis.data_quality} />
            </Section>
          )}
        </div>
      </details>

      <p className="border-t border-border pt-3 text-xs text-muted">
        本分析用于辅助识别盘中结构，不构成投资建议，也不改变正式买卖信号。
      </p>
    </div>
  )
}
