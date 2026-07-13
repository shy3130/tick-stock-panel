import { useState } from 'react'
import { PageHeader } from '@/components/PageHeader'
import { FactorBacktest } from './backtest/FactorBacktest'
import { StrategyBacktest } from './backtest/StrategyBacktest'
import { StrategyOptimizer } from './backtest/StrategyOptimizer'
import { StrategyWalkForward } from './backtest/StrategyWalkForward'
import { BarChart3, FlaskConical, SlidersHorizontal, Waypoints } from 'lucide-react'

type Tab = 'factor' | 'strategy' | 'optimizer' | 'walkforward'

const MODES: Record<Tab, { title: string; subtitle: string; hint: string }> = {
  factor: {
    title: '因子回测',
    subtitle: '验证单个因子是否有预测能力',
    hint: '看 IC / IR、分层收益和多空组合，适合先筛掉无效指标。',
  },
  strategy: {
    title: '策略回测',
    subtitle: '验证完整选股和交易规则',
    hint: '看净值曲线、回撤、胜率和交易明细，适合评估策略的历史表现。',
  },
  optimizer: {
    title: '参数优化',
    subtitle: '网格搜索最优参数组合',
    hint: '并行回测所有参数组合，按夏普/索提诺等目标排序，找到最优参数。',
  },
  walkforward: {
    title: 'Walk-forward',
    subtitle: '滚动窗口样本外验证',
    hint: '每折训练区间优化、测试区间验证，看样本外是否退化以识别过拟合。',
  },
}

const TAB_ICONS: Record<Tab, typeof BarChart3> = {
  factor: BarChart3,
  strategy: FlaskConical,
  optimizer: SlidersHorizontal,
  walkforward: Waypoints,
}

export function Backtest() {
  const [activeTab, setActiveTab] = useState<Tab>('strategy')

  const modeSwitch = (
    <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5 shadow-sm">
      {(['factor', 'strategy', 'optimizer', 'walkforward'] as const).map(tab => {
        const Icon = TAB_ICONS[tab]
        const active = activeTab === tab
        return (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors cursor-pointer ${
              active
                ? 'bg-accent text-white shadow-sm'
                : 'text-secondary hover:bg-elevated hover:text-foreground'
            }`}
          >
            <Icon className="h-3.5 w-3.5" />
            {MODES[tab].title}
            {tab === 'optimizer' && (
              <span className={`rounded border px-1 py-px text-[8px] font-semibold uppercase ${
                active ? 'border-white/40 bg-white/15 text-white' : 'border-amber-400/30 bg-amber-400/10 text-amber-400'
              }`}>
                Beta
              </span>
            )}
          </button>
        )
      })}
    </div>
  )

  return (
    <div className="min-h-full bg-base flex flex-col">
      <PageHeader
        title="回测工作台"
        subtitle={`${MODES[activeTab].title} · ${MODES[activeTab].hint}`}
        right={modeSwitch}
        className="shrink-0 bg-base/95"
      />

      <main className="flex-1 min-h-0 px-3 pb-3 pt-3 lg:px-4 lg:pb-4">
        {activeTab === 'factor' && <FactorBacktest />}
        {activeTab === 'strategy' && <StrategyBacktest />}
        {activeTab === 'optimizer' && <StrategyOptimizer />}
        {activeTab === 'walkforward' && <StrategyWalkForward />}
      </main>
    </div>
  )
}
