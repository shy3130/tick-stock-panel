/**
 * 因子页 — 按任务组织的三视图: 发现 (检验+挖掘) / 创建 (公式+组合) / 因子库 (管理)。
 *
 * 历史 5 平级 tab (检验/因子库/编辑器/组合/挖掘) 是三种任务被摊开: 评估类散在
 * 两处、创建类散在两处。收敛后子视图各自有分段切换, 旧 tab 参数经 LEGACY_TABS
 * 映射继续工作 (router 的 /mining 与回测页重定向不受影响)。
 */
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { BookmarkCheck, FlaskConical, LibrarySquare, Search } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { FactorDiscovery } from './backtest/FactorDiscovery'
import { FactorComposite } from './factors/FactorComposite'
import { FactorEditor } from './factors/FactorEditor'
import { FactorLibrary } from './factors/FactorLibrary'
import { ResearchCandidatesDialog } from './backtest/ResearchCandidatesDialog'

type Tab = 'discover' | 'create' | 'library'
type CreateMode = 'editor' | 'composite'

const TABS: Record<Tab, { title: string; icon: typeof Search; subtitle: string }> = {
  discover: { title: '发现', icon: Search, subtitle: '批量检验与挖掘: 哪些特征历史上与涨跌相关' },
  create: { title: '创建', icon: FlaskConical, subtitle: '写公式定义因子, 或勾选现有因子组合' },
  library: { title: '因子库', icon: LibrarySquare, subtitle: '因子注册表目录与生命周期管理' },
}

// 旧五 tab → 新三 tab; view/mode 让旧深链 (如 tab=mining&run=xx) 落到正确子视图
const LEGACY_TABS: Record<string, { tab: Tab; view?: string; mode?: CreateMode }> = {
  inspect: { tab: 'discover' },
  mining: { tab: 'discover', view: 'mining' },
  editor: { tab: 'create', mode: 'editor' },
  composite: { tab: 'create', mode: 'composite' },
  library: { tab: 'library' },
}

const CREATE_MODES: { id: CreateMode; label: string }[] = [
  { id: 'editor', label: '公式编辑器' },
  { id: 'composite', label: '因子组合' },
]

export function Factors() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [candidatesOpen, setCandidatesOpen] = useState(false)
  const navigate = useNavigate()

  const requestedTab = searchParams.get('tab')
  const legacy = requestedTab ? LEGACY_TABS[requestedTab] : undefined
  const activeTab: Tab = requestedTab && requestedTab in TABS
    ? requestedTab as Tab
    : legacy?.tab ?? 'discover'
  const requestedView = searchParams.get('view') ?? legacy?.view
  const discoverView = requestedView === 'single' || requestedView === 'mining' ? requestedView : 'batch'
  const requestedMode = (searchParams.get('mode') ?? legacy?.mode) as CreateMode | null
  const createMode: CreateMode = requestedMode === 'composite' ? 'composite' : 'editor'
  const focusFactor = activeTab === 'discover' ? searchParams.get('focus') ?? '' : ''
  const editId = searchParams.get('edit') ?? ''

  const patchParams = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams)
    for (const [key, value] of Object.entries(patch)) {
      if (value == null) next.delete(key)
      else next.set(key, value)
    }
    setSearchParams(next, { replace: true })
  }

  const changeTab = (tab: Tab) => {
    patchParams({ tab, focus: null, edit: null, view: null, mode: null })
  }

  return (
    <div className="flex min-h-full flex-col bg-base xl:h-full">
      <PageHeader
        title="因子"
        subtitle={<span className="hidden md:inline">{TABS[activeTab].subtitle}</span>}
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
            <nav className="min-w-0 flex-1 overflow-x-auto lg:flex-none" aria-label="因子视图">
              <div className="inline-flex min-w-max items-center gap-0.5 rounded-btn border border-border bg-surface/80 p-0.5">
                {(Object.keys(TABS) as Tab[]).map(tab => {
                  const mode = TABS[tab]
                  const Icon = mode.icon
                  const active = activeTab === tab
                  return (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => changeTab(tab)}
                      aria-current={active ? 'page' : undefined}
                      className={`inline-flex h-7 items-center gap-1 rounded-[5px] px-2.5 text-xs font-medium transition-colors ${active
                        ? 'bg-accent text-white shadow-sm'
                        : 'text-secondary hover:bg-elevated hover:text-foreground'
                      }`}
                    >
                      <Icon className="h-3.5 w-3.5" />
                      {mode.title}
                    </button>
                  )
                })}
              </div>
            </nav>
          </div>
        )}
      />

      <main className="min-h-0 flex-1 px-3 pb-3 pt-3 lg:px-4 lg:pb-4 xl:overflow-hidden">
        {activeTab === 'discover' && (
          <FactorDiscovery focusFactor={focusFactor} initialView={discoverView} />
        )}
        {activeTab === 'create' && (
          <div className="flex h-full min-h-0 flex-col gap-3">
            <div className="flex shrink-0 items-center px-1">
              <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
                {CREATE_MODES.map(({ id, label }) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => patchParams({ mode: id, edit: null })}
                    aria-current={createMode === id ? 'true' : undefined}
                    className={`inline-flex h-7 items-center gap-1.5 rounded-[5px] px-3 text-xs font-medium transition-colors ${createMode === id
                      ? 'bg-accent text-white shadow-sm'
                      : 'text-secondary hover:bg-elevated hover:text-foreground'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {createMode === 'editor' && editId && (
                <button
                  type="button"
                  onClick={() => patchParams({ edit: null })}
                  className="ml-2 inline-flex items-center gap-1 rounded-btn px-2 py-1 text-[11px] text-muted transition-colors hover:bg-elevated hover:text-foreground"
                  title="退出编辑, 回到新建"
                >
                  ← 新建因子
                </button>
              )}
            </div>
            <div className="min-h-0 flex-1">
              {createMode === 'editor'
                ? <FactorEditor key={editId} editId={editId} />
                : <FactorComposite />}
            </div>
          </div>
        )}
        {activeTab === 'library' && (
          <FactorLibrary
            onInspect={factorId => patchParams({ tab: 'discover', view: 'batch', focus: factorId, edit: null, mode: null })}
            onEdit={factorId => patchParams({ tab: 'create', mode: 'editor', edit: factorId, focus: null, view: null })}
          />
        )}
      </main>

      {candidatesOpen && (
        <ResearchCandidatesDialog
          onClose={() => setCandidatesOpen(false)}
          onLoadStrategy={candidate => {
            setCandidatesOpen(false)
            // 跨页「载入复测」: 候选经 router state 交给回测策略页消费
            navigate('/backtest?tab=strategy', { state: { loadCandidate: candidate } })
          }}
        />
      )}
    </div>
  )
}
