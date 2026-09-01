import { PageHeader } from '@/components/PageHeader'
import { HypothesesPanel } from '../components/ResearchNotebook'

export function EvidencePage() {
  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader title="研究证据" subtitle="假设与证据链仍走既有 hypotheses / run-card 接口；factor Run 不冒充旧 run-card" />
      <div className="workspace-content">
        <div className="mx-auto max-w-6xl">
          <HypothesesPanel />
        </div>
      </div>
    </div>
  )
}
