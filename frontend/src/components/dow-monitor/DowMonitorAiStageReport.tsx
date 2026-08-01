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

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h4 className="font-medium">{title}</h4>
      <div className="mt-2 text-secondary leading-6">{children}</div>
    </section>
  )
}

function List({ items }: { items: string[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5">
      {items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
    </ul>
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
  return (
    <div className="space-y-5 text-sm">
      <section>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <span>小时阶段分析</span>
          <span>{CHANGE_LABELS[report.headline.opportunity_change]}</span>
          <span>
            北京时间 {start?.slice(11) ?? '--'} 至 {cutoff?.slice(11) ?? '--'}
          </span>
          {analysis.stage_trading_minutes != null && (
            <span>{analysis.stage_trading_minutes} 个交易分钟</span>
          )}
        </div>
        <h3 className="mt-1 text-lg font-semibold">{report.headline.title}</h3>
        <p className="mt-2 leading-6 text-secondary">{report.headline.summary}</p>
      </section>

      <Section title="本阶段分钟路径">
        <div className="space-y-2">
          {report.stage_path.map((item, index) => (
            <div key={`${item.period}-${index}`} className="rounded-card bg-elevated p-3">
              <strong>{item.period}</strong>
              <p>{item.description}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section title="分钟K线隐藏变化"><List items={report.hidden_changes} /></Section>
      <Section title="与上一阶段相比"><p>{report.comparison_with_previous}</p></Section>
      <Section title="当日截至当前"><p>{report.day_overview}</p></Section>

      <Section title="通道与形态">
        <div className="rounded-card border border-border p-3">
          <strong>{DIRECTION_LABELS[report.channel.direction]}</strong>
          <p>{report.channel.explanation}</p>
        </div>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {report.patterns.map((pattern, index) => (
            <div key={`${pattern.name}-${index}`} className="rounded-card border border-border p-3">
              <strong>{pattern.name}</strong>
              <p>{pattern.explanation}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section title="量价与资金含义"><p>{report.volume_capital_interpretation}</p></Section>
      <Section title="持仓者建议">
        <p>{report.holding_advice.advice}</p>
        <List items={report.holding_advice.conditions} />
      </Section>
      <Section title="未参与者建议">
        <p>{report.watching_advice.advice}</p>
        <List items={report.watching_advice.conditions} />
      </Section>
      <Section title="下一阶段条件">
        <div className="grid gap-2 sm:grid-cols-3">
          <div><strong>增强确认</strong><List items={report.next_stage_conditions.strengthen} /></div>
          <div><strong>风险出现</strong><List items={report.next_stage_conditions.risk} /></div>
          <div><strong>判断失效</strong><List items={report.next_stage_conditions.invalidation} /></div>
        </div>
      </Section>
      <Section title="数据质量"><p>{analysis.data_quality.join('；')}</p></Section>
      <p className="border-t border-border pt-3 text-xs text-muted">
        本分析用于辅助识别盘中结构，不构成投资建议，也不改变正式买卖信号。
      </p>
    </div>
  )
}
