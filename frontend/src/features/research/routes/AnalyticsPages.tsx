import { PageHeader } from '@/components/PageHeader'
import { AnalysisPanel } from '@/components/research/AnalysisPanel'
import { CrossSection } from '@/pages/CrossSection'
import { SignalScorecard } from '@/pages/SignalScorecard'

export function AnalyticsSymbolPage() {
  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader title="单标的分析" subtitle="独立 ADF/GARCH/风险工具，不进入 Factor Registry，也不伪装成 factor run" />
      <div className="workspace-content">
        <div className="mx-auto max-w-6xl">
          <AnalysisPanel />
        </div>
      </div>
    </div>
  )
}

export function AnalyticsSignalsPage() {
  return <SignalScorecard />
}

export function AnalyticsCrossSectionPage() {
  return <CrossSection />
}
