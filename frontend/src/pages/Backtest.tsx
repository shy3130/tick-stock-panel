import { useEffect, useState } from 'react'
import { Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { BookmarkCheck, FlaskConical, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { ResearchCandidatesDialog } from './backtest/ResearchCandidatesDialog'
import { RobustnessValidation } from './backtest/RobustnessValidation'
import { StrategyBacktest } from './backtest/StrategyBacktest'
import { type ResearchCandidate } from '@/lib/api'

type Tab = 'strategy' | 'robustness'

const MODES: Record<Tab, { title: string; subtitle: string; icon: typeof FlaskConical }> = {
  strategy: {
    title: '策略',
    subtitle: '现有策略评估与候选沉淀',
    icon: FlaskConical,
  },
  robustness: {
    title: '验证',
    subtitle: '稳健参数筛选与滚动样本外检验',
    icon: ShieldCheck,
  },
}

export function Backtest() {
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const requestedTab = searchParams.get('tab')
  const [candidatesOpen, setCandidatesOpen] = useState(false)
  // 候选「载入复测」: 弹窗选定 → 关闭弹窗切到策略页 → StrategyBacktest 消费后清空
  const [pendingLoad, setPendingLoad] = useState<ResearchCandidate | null>(null)

  // 跨页「载入复测」: 因子页候选弹窗经 router state 传入 (location.state), 消费后清除防止刷新重复载入
  const stateCandidate = (location.state as { loadCandidate?: ResearchCandidate } | null)?.loadCandidate ?? null
  useEffect(() => {
    if (!stateCandidate) return
    setPendingLoad(stateCandidate)
    navigate({ pathname: location.pathname, search: location.search }, { replace: true })
  }, [stateCandidate, location.pathname, location.search, navigate])

  // 旧链接兼容: 因子已升级为一级路由 /factors, 保留其余参数重定向
  if (requestedTab === 'factor') {
    const next = new URLSearchParams(searchParams)
    next.delete('tab')
    const search = next.toString()
    return <Navigate to={search ? `/factors?${search}` : '/factors'} replace />
  }

  // 旧链接兼容: 挖掘已并入因子页 (/factors?tab=mining), 保留 run/candidate 参数重定向
  if (requestedTab === 'mining') {
    const next = new URLSearchParams(searchParams)
    next.set('tab', 'mining')
    return <Navigate to={`/factors?${next.toString()}`} replace />
  }

  const activeTab: Tab = requestedTab && requestedTab in MODES
    ? requestedTab as Tab
    : 'strategy'

  const changeTab = (tab: Tab) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', tab)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="flex min-h-full flex-col bg-base">
      <PageHeader
        title="回测"
        subtitle={<span className="hidden md:inline">{MODES[activeTab].subtitle}</span>}
        className="shrink-0 flex-wrap gap-x-4 gap-y-2 bg-base/95 px-3 lg:flex-nowrap lg:px-5"
        right={(
          <div className="flex w-full min-w-0 items-center gap-1.5 sm:gap-2 lg:w-auto">
            <button
              type="button"
              onClick={() => setCandidatesOpen(true)}
              aria-label="打开候选方案"
              title="候选方案"
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-[11px] font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent sm:px-2.5 sm:text-xs"
            >
              <BookmarkCheck className="h-3.5 w-3.5" />
              <span>候选方案</span>
            </button>
            <span className="h-5 w-px shrink-0 bg-border" aria-hidden="true" />
            <nav className="min-w-0 flex-1 overflow-x-auto lg:flex-none" aria-label="回测视图">
              <div className="inline-flex min-w-max items-center gap-0.5 rounded-btn border border-border bg-surface/80 p-0.5">
                {(Object.keys(MODES) as Tab[]).map(tab => {
                  const mode = MODES[tab]
                  const Icon = mode.icon
                  const active = activeTab === tab
                  return (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => changeTab(tab)}
                      aria-current={active ? 'page' : undefined}
                      className={`inline-flex h-7 items-center gap-1 rounded-[5px] px-1.5 text-[11px] font-medium transition-colors sm:gap-1.5 sm:px-2.5 sm:text-xs ${active
                        ? 'bg-accent text-white shadow-sm'
                        : 'text-secondary hover:bg-elevated hover:text-foreground'
                      }`}
                    >
                      <Icon className="hidden h-3.5 w-3.5 sm:block" />
                      {mode.title}
                    </button>
                  )
                })}
              </div>
            </nav>
          </div>
        )}
      />

      <main className="min-h-0 flex-1 px-3 pb-3 pt-3 lg:px-4 lg:pb-4">
        {activeTab === 'strategy' && (
          <StrategyBacktest
            loadCandidate={pendingLoad}
            onLoadConsumed={() => setPendingLoad(null)}
          />
        )}
        {activeTab === 'robustness' && <RobustnessValidation />}
      </main>

      {candidatesOpen && (
        <ResearchCandidatesDialog
          onClose={() => setCandidatesOpen(false)}
          onLoadStrategy={candidate => {
            setPendingLoad(candidate)
            setCandidatesOpen(false)
            if (activeTab !== 'strategy') changeTab('strategy')
          }}
        />
      )}
    </div>
  )
}
