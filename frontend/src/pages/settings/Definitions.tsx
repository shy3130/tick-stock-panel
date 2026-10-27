/**
 * 统一定义目录面板 — "一切皆扩展"的消费面 (P4)。
 *
 * 跨 kind (信号/因子) 的统一目录 + 逐条导入冒烟。定义 (信号/因子/策略/
 * 数据集) 都走同一条 契约校验 → 编译 → 依赖可解析 链路; 这里是它们的
 * 总目录与体检入口, 也是配置备份导入后核对冒烟结果的落点。
 */
import { Library } from 'lucide-react'
import { DefinitionPicker } from '@/components/DefinitionPicker'
import { PositionPoliciesPanel } from '@/components/settings/PositionPoliciesPanel'

export function SettingsDefinitionsPanel() {
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <PositionPoliciesPanel />
      <div className="rounded-card border border-border bg-surface p-4">
        <div className="flex items-start gap-2.5">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-accent/10 text-accent">
            <Library className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">统一定义目录</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
              自定义信号与因子的统一视图 (语言 / 依赖 / 预热 / 状态)。
              「冒烟」当场执行 校验 → 编译 → 依赖可解析, 拼错列名 (W001)
              无需跑回测即可发现; 导入配置备份时同一检查自动执行。
            </div>
          </div>
        </div>
        <div className="mt-3">
          <DefinitionPicker kinds={['signal', 'factor']} />
        </div>
      </div>
    </div>
  )
}
