import { PageHeader } from 'tickflow-stock-panel-frontend'

export function Default() {
  return <PageHeader title="自选股" />
}

export function WithSubtitle() {
  return <PageHeader title="回测报告" subtitle="策略:双均线金叉 · 2023-01-01 ~ 2024-12-31" />
}

export function WithRightSlot() {
  return (
    <PageHeader
      title="个股分析"
      subtitle="600519.SH 贵州茅台"
      right={
        <button
          type="button"
          style={{
            height: 28, padding: '0 12px', borderRadius: 6,
            background: 'hsl(217 91% 60%)', color: '#fff',
            fontSize: 12, fontWeight: 500, border: 'none',
          }}
        >
          加自选
        </button>
      }
    />
  )
}
