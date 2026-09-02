import { PageHeader } from '@/components/PageHeader'
import { MarketDataPanel } from '@/components/research/MarketDataPanel'

export function DataLineagePage() {
  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader title="市场数据谱系" subtitle="只读上游发布快照；用户显式查询，不在前端连接 DuckDB" />
      <div className="workspace-content">
        <div className="mx-auto max-w-6xl">
          <MarketDataPanel />
        </div>
      </div>
    </div>
  )
}
