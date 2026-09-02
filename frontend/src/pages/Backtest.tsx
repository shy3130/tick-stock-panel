import { useCallback, useEffect, useState, type KeyboardEvent } from 'react'
import { storage } from '@/lib/storage'
import {
  clearScreenerBacktestHandoff,
  peekScreenerBacktestHandoff,
  type ScreenerBacktestHandoff,
} from '@/lib/screenerBacktestHandoff'
import { PageHeader } from '@/components/PageHeader'
import { FactorBacktest } from './backtest/FactorBacktest'
import { StrategyBacktest, type StrategyParameterBackfill } from './backtest/StrategyBacktest'
import { CompositeStrategyBuilder } from './backtest/CompositeStrategyBuilder'
import { PortfolioCombinePanel } from './backtest/PortfolioCombinePanel'
import { ParameterGridPanel } from './backtest/ParameterGridPanel'
import { StrategySearchPanel } from './backtest/StrategySearchPanel'
import { RunHistoryPanel } from './backtest/RunHistoryPanel'
import { BarChart3, FlaskConical, GitMerge, Grid3X3, History, Search, type LucideIcon } from 'lucide-react'

const TABS = ['factor', 'strategy', 'composite', 'grid', 'search', 'history'] as const
type Tab = typeof TABS[number]

const TAB_ICONS: Record<Tab, LucideIcon> = {
  factor: BarChart3,
  strategy: FlaskConical,
  composite: GitMerge,
  grid: Grid3X3,
  search: Search,
  history: History,
}

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
  search: {
    title: '策略寻优',
    subtitle: '跨策略、股票池与周期的训练/留出搜索',
    hint: '最近 8 年冻结窗口，只按训练期打分，留出期确认；输出 DSR/PBO，不宣称全局最优。',
  },
  history: {
    title: '运行历史',
    subtitle: '持久化回测记录的检索与对比',
    hint: '搜索、收藏、打标签，选择 2~8 次运行做指标与净值对比，可导出自包含对比报告。',
  },
}

const MODE_GROUPS: { id: string; label: string; tabs: Tab[] }[] = [
  { id: 'verify', label: '验证', tabs: ['factor', 'strategy', 'composite'] },
  { id: 'experiment', label: '实验', tabs: ['grid', 'search'] },
  { id: 'asset', label: '资产', tabs: ['history'] },
]

export function Backtest() {
  const [screenerHandoff, setScreenerHandoff] = useState<ScreenerBacktestHandoff | null>(
    () => peekScreenerBacktestHandoff(),
  )
  const [activeTab, setActiveTab] = useState<Tab>(() => {
    if (screenerHandoff) return screenerHandoff.target
    const savedTab = storage.backtestActiveTab.get('strategy')
    return TABS.includes(savedTab) ? savedTab : 'strategy'
  })
  const [parameterBackfill, setParameterBackfill] = useState<StrategyParameterBackfill | null>(null)

  useEffect(() => {
    if (screenerHandoff) clearScreenerBacktestHandoff()
  }, [screenerHandoff])
  const selectTab = (tab: Tab) => {
    setActiveTab(tab)
    storage.backtestActiveTab.set(tab)
  }
  const clearScreenerHandoff = useCallback(() => setScreenerHandoff(null), [])
  const useGridScenario = useCallback((strategyId: string, params: Record<string, number>) => {
    setParameterBackfill({ strategyId, params, revision: Date.now() })
    setActiveTab('strategy')
    storage.backtestActiveTab.set('strategy')
  }, [])
  const useSearchStrategy = useCallback((strategyId: string) => {
    setParameterBackfill({ strategyId, params: {}, revision: Date.now() })
    setActiveTab('strategy')
    storage.backtestActiveTab.set('strategy')
  }, [])
  const scenarioRunComplete = useCallback(() => {
    setActiveTab('history')
    storage.backtestActiveTab.set('history')
  }, [])

  /** F7: 实验区「打开」→ 写恢复键已由 task 模块完成, 这里切换到对应实验 tab */
  const openExperimentTab = useCallback((kind: 'optimizer' | 'grid') => {
    selectTab(kind === 'optimizer' ? 'search' : 'grid')
  }, [])
  const clearParameterBackfill = useCallback(() => setParameterBackfill(null), [])

  const mode = MODES[activeTab]

  const onModeKeyDown = (event: KeyboardEvent<HTMLButtonElement>, tab: Tab) => {
    const index = TABS.indexOf(tab)
    let nextIndex = -1
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % TABS.length
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + TABS.length) % TABS.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = TABS.length - 1
    if (nextIndex < 0) return
    event.preventDefault()
    const next = TABS[nextIndex]
    selectTab(next)
    requestAnimationFrame(() => {
      document.getElementById(`backtest-mode-${next}`)?.focus()
    })
  }

  return (
    <div className="workspace-page overflow-x-hidden">
      <PageHeader
        title="回测工作台"
        subtitle={`${mode.title} · ${mode.subtitle}`}
      />

      <div className="min-w-0 border-b border-border bg-surface px-3 py-1.5 sm:px-4">
        <div
          role="tablist"
          aria-label="回测模式"
          className="flex min-w-0 items-center gap-2 overflow-x-auto"
        >
          {MODE_GROUPS.map((group, groupIndex) => (
            <div
              key={group.id}
              role="presentation"
              className="flex shrink-0 items-center gap-1.5"
            >
              {groupIndex > 0 ? (
                <span className="mr-1 h-5 w-px shrink-0 bg-border" aria-hidden="true" />
              ) : null}
              <span className="shrink-0 text-[10px] font-medium text-muted" aria-hidden="true">
                {group.label}
              </span>
              {group.tabs.map(tab => {
                const Icon = TAB_ICONS[tab]
                const active = activeTab === tab
                return (
                  <button
                    key={tab}
                    id={`backtest-mode-${tab}`}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    tabIndex={active ? 0 : -1}
                    onClick={() => selectTab(tab)}
                    onKeyDown={event => onModeKeyDown(event, tab)}
                    className={`${active ? 'btn-primary' : 'btn-ghost'} !h-9 min-h-9 px-2.5 text-xs focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent`}
                  >
                    <Icon className="h-3.5 w-3.5" aria-hidden="true" />
                    {MODES[tab].title}
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <p className="mt-1.5 min-w-0 text-[11px] leading-snug text-muted">
          {mode.hint}
        </p>
      </div>

      <div className="workspace-content !pt-0 min-h-0 flex-1 flex flex-col">
        <div className="flex-1 min-h-0 min-w-0">
          {activeTab === 'factor' && (
            <FactorBacktest
              screenerHandoff={screenerHandoff?.target === 'factor' ? screenerHandoff : null}
              onScreenerHandoffApplied={clearScreenerHandoff}
            />
          )}
          {activeTab === 'strategy' && (
            <StrategyBacktest
              screenerHandoff={screenerHandoff?.target === 'strategy' ? screenerHandoff : null}
              onScreenerHandoffApplied={clearScreenerHandoff}
              parameterBackfill={parameterBackfill}
              onParameterBackfillApplied={clearParameterBackfill}
            />
          )}
          {activeTab === 'composite' && (
            <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto">
              <div className="min-h-[34rem] shrink-0">
                <CompositeStrategyBuilder />
              </div>
              <PortfolioCombinePanel />
            </div>
          )}
          <div className={activeTab === 'grid' ? 'contents' : 'hidden'} aria-hidden={activeTab !== 'grid'}>
            <ParameterGridPanel onUseScenario={useGridScenario} onScenarioRunComplete={scenarioRunComplete} />
          </div>
          <div className={activeTab === 'search' ? 'contents' : 'hidden'} aria-hidden={activeTab !== 'search'}>
            <StrategySearchPanel onUseScenario={useSearchStrategy} onScenarioRunComplete={scenarioRunComplete} />
          </div>
          {activeTab === 'history' && <RunHistoryPanel onOpenExperiment={openExperimentTab} />}
        </div>
      </div>
    </div>
  )
}
