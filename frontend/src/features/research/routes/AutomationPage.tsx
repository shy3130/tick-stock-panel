import { PageHeader } from '@/components/PageHeader'
import { SchedulesPanel } from '../components/ResearchNotebook'

export function AutomationPage() {
  return (
    <div className="workspace-page h-full min-h-0 overflow-auto">
      <PageHeader
        title="定时研究"
        subtitle="复盘模板与冻结参数的因子运行并列创建。factor_run 生成 Durable Run，三类 recap 仍写入既有 Run Card。"
      />
      <div className="workspace-content">
        <div className="mx-auto grid min-w-0 max-w-6xl gap-6 lg:grid-cols-2">
          <SchedulesPanel kind="recap" />
          <SchedulesPanel kind="factor_run" />
        </div>
      </div>
    </div>
  )
}
