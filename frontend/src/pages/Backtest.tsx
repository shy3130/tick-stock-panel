import { useState } from 'react'
import { PageHeader } from '@/components/PageHeader'
import { FactorBacktest } from './backtest/FactorBacktest'
import { StrategyBacktest } from './backtest/StrategyBacktest'
import { CompositeStrategyBuilder } from './backtest/CompositeStrategyBuilder'
import { ParameterGridPanel } from './backtest/ParameterGridPanel'
import { BarChart3, FlaskConical, GitMerge, Grid3X3 } from 'lucide-react'

type Tab = 'factor' | 'strategy' | 'composite' | 'grid'

const MODES: Record<Tab, { title: string; subtitle: string; hint: string }> = {
  factor: {
    title: '因子回测',
    subtitle: '验证单个因子是否有预测能力',
    hint: '看 IC / IR、分层收益和多空组合，适合先筛掉无效指标。',
  },
  strategy: {
    title: '策略回测',
    subtitle: '验证完整选股和交易规则',
    hint: '看净值曲线、回撤、胜率和交易明细，适合判断策略是否可执行。',
  },
  composite: {
    title: '组合策略',
    subtitle: '声明式合并多个现有策略',
    hint: '配置并集、交集、确认数和权重，保存后仍通过既有策略回测验证。',
  },
  grid: {
    title: '参数网格',
    subtitle: '批量比较有限参数组合',
    hint: '运行本地历史场景，查看排序、稳健性和过拟合风险。',
  },
}

export function Backtest() {
  const [activeTab, setActiveTab] = useState<Tab>('strategy')

  const modeSwitch = (
    <div className="workspace-toolbar !border-0 !bg-transparent !px-0 !py-0 !mb-0" role="group" aria-label="回测模式">
      <div className="inline-flex rounded-btn border border-border bg-elevated p-0.5">
        {(['factor', 'strategy', 'composite', 'grid'] as const).map(tab => {
          const Icon = tab === 'factor'
            ? BarChart3
            : tab === 'strategy'
              ? FlaskConical
              : tab === 'composite'
                ? GitMerge
                : Grid3X3
          const active = activeTab === tab
          return (
            <button
              key={tab}
              type="button"
              aria-pressed={active}
              onClick={() => setActiveTab(tab)}
              className={`inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1.5 text-xs font-medium transition-colors duration-150 cursor-pointer ${
                active
                  ? 'bg-accent text-white shadow-sm'
                  : 'text-secondary hover:bg-surface hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {MODES[tab].title}
            </button>
          )
        })}
      </div>
    </div>
  )

  return (
    <div className="workspace-page">
      <PageHeader
        title="回测工作台"
        subtitle={`${MODES[activeTab].title} · ${MODES[activeTab].subtitle}`}
        titleExtra={
          <span className="hidden sm:inline text-[11px] text-muted font-normal max-w-md truncate">
            {MODES[activeTab].hint}
          </span>
        }
        right={modeSwitch}
      />

      <div className="workspace-content !pt-0 min-h-0 flex-1 flex flex-col">
        <div className="flex-1 min-h-0 min-w-0">
          {activeTab === 'factor' && <FactorBacktest />}
          {activeTab === 'strategy' && <StrategyBacktest />}
          {activeTab === 'composite' && <CompositeStrategyBuilder />}
          <div className={activeTab === 'grid' ? 'contents' : 'hidden'} aria-hidden={activeTab !== 'grid'}>
            <ParameterGridPanel />
          </div>
        </div>
      </div>
    </div>
  )
}
