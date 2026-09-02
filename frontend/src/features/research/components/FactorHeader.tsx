import { Link } from 'react-router-dom'
import { Panel, PanelBody, PanelHeader } from '@/components/ui/Primitives'
import { fmtDateTime } from '../lib/format'
import type { FactorDetail } from '../model/factor'
import { DataStatusBadge, EngineeringBadge, ProfileBadge, PromotionBadge, VerdictBadge } from './StatusBadges'

export function FactorHeader({ detail }: { detail: FactorDetail }) {
  return (
    <Panel>
      <PanelHeader>
        <div className="min-w-0">
          <p className="section-kicker">{detail.category}</p>
          <h2 className="section-title truncate">{detail.title}</h2>
          <p className="mt-0.5 font-mono text-[11px] text-muted">{detail.id}</p>
        </div>
      </PanelHeader>
      <PanelBody className="space-y-3">
        <p className="text-xs leading-relaxed text-secondary">{detail.description || '服务端未提供描述。'}</p>
        <div className="flex flex-wrap gap-1.5">
          <EngineeringBadge value={detail.engineering_status} />
          <DataStatusBadge value={detail.latest_data_status} />
          <VerdictBadge value={detail.latest_verdict} />
          <PromotionBadge value={detail.promotion_status} />
          <ProfileBadge value={detail.result_profile} />
        </div>
        {detail.arms.length > 0 ? (
          <div>
            <p className="text-[11px] font-medium text-muted">Arms</p>
            <ul className="mt-1 space-y-1 text-xs text-secondary">
              {detail.arms.map((arm) => (
                <li key={arm.id}><span className="font-mono text-muted">{arm.id}</span> {arm.title}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {detail.strongest_baseline ? (
          <p className="text-xs text-secondary">最强基线：<span className="font-mono">{detail.strongest_baseline}</span></p>
        ) : null}
        {detail.acceptance_gates.length > 0 ? (
          <div>
            <p className="text-[11px] font-medium text-muted">Acceptance gates</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-secondary">
              {detail.acceptance_gates.map((gate) => <li key={gate.id}>{gate.title}</li>)}
            </ul>
          </div>
        ) : null}
        {detail.known_gaps.length > 0 ? (
          <div>
            <p className="text-[11px] font-medium text-muted">已知缺口</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-warning">
              {detail.known_gaps.map((gap) => <li key={gap}>{gap}</li>)}
            </ul>
          </div>
        ) : null}
        {detail.latest_runs.length > 0 ? (
          <div>
            <p className="text-[11px] font-medium text-muted">历史运行</p>
            <ul className="mt-1 space-y-1 text-xs">
              {detail.latest_runs.slice(0, 6).map((run) => (
                <li key={run.run_id}>
                  <Link className="font-mono text-accent hover:underline" to={`/research/runs/${encodeURIComponent(run.run_id)}`}>
                    {run.run_id}
                  </Link>
                  <span className="ml-2 text-muted">{fmtDateTime(run.created_at)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </PanelBody>
    </Panel>
  )
}
