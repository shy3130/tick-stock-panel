/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Database, Radio, SlidersHorizontal, Sparkles, Settings2, UserCog, Zap } from 'lucide-react'
import { SettingsKeysPanel } from './settings/Keys'
import { SettingsAIPanel } from './settings/AI'
import { SettingsMonitoringPanel } from './settings/Monitoring'
import { SettingsExtPagesPanel } from './settings/ExtPages'
import { SettingsMenuSettingsPanel } from './settings/MenuSettings'
import { SettingsSystemPanel } from './settings/System'
import { SettingsCustomSignalsPanel } from './settings/CustomSignals'
import { SettingsDataSourcesPanel } from './settings/DataSources'
import { SettingsAccountPanel } from './settings/Account'
import { api } from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

import type { ComponentType } from 'react'

// ===== Tab 定义 =====

type TabDef = {
  key: string
  label: string
  group: TabGroupKey
  icon: ComponentType<{ className?: string }>
  panel: ComponentType<{ highlight?: string }>
  badge?: string
}

type TabGroupKey = 'services' | 'workspace' | 'system'

const TAB_GROUPS: readonly { key: TabGroupKey; label: string }[] = [
  { key: 'services', label: '服务' },
  { key: 'workspace', label: '工作台' },
  { key: 'system', label: '系统' },
]

const TABS: readonly TabDef[] = [
  { key: 'ai',           label: 'AI 设置',  group: 'services', icon: Sparkles, panel: SettingsAIPanel },
  { key: 'monitoring',   label: '实时监控', group: 'services', icon: Radio, panel: SettingsMonitoringPanel },
  { key: 'data-sources', label: '数据源',   group: 'services', icon: Database, panel: SettingsDataSourcesPanel, badge: 'BETA' },
  { key: 'ext-pages',    label: '扩展页面', group: 'workspace', icon: BarChart3, panel: SettingsExtPagesPanel },
  { key: 'signals',      label: '信号库',   group: 'workspace', icon: Zap, panel: SettingsCustomSignalsPanel },
  { key: 'menus',        label: '菜单设置', group: 'workspace', icon: SlidersHorizontal, panel: SettingsMenuSettingsPanel },
  { key: 'system',       label: '系统设置', group: 'system', icon: Settings2, panel: SettingsSystemPanel },
  { key: 'account',      label: '账户管理', group: 'system', icon: UserCog, panel: SettingsAccountPanel },
]

type TabKey = (typeof TABS)[number]['key']

export function Settings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const authStatus = useQuery({ queryKey: ['auth-status'], queryFn: api.authStatus, staleTime: 30_000 })
  const isAdmin = authStatus.data?.user?.role === 'admin'
  const visibleTabs = isAdmin ? TABS : TABS.filter((tab) => tab.key !== 'account' && tab.key !== 'data-sources')
  const tabParam = searchParams.get('tab') as TabKey | null
  const activeTab = visibleTabs.find((t) => t.key === tabParam) ?? visibleTabs[0]
  // Key 配置仍可从数据页的上下文入口访问，但不再占用设置主导航。
  const showingAccountPanel = tabParam === 'account'
  const highlight = searchParams.get('highlight') ?? ''

  return (
    <>
      <PageHeader
        title="设置"
        subtitle="管理账户、数据刷新策略和高级功能配置。"
      />

      <div className="px-3 py-4 sm:px-5">
        <div className="grid min-w-0 gap-5 lg:grid-cols-[12rem_minmax(0,1fr)] lg:items-start">
          {/* ===== 设置导航：桌面分组侧栏，小屏横向滚动 ===== */}
          <nav aria-label="设置菜单" className="min-w-0 lg:border-r lg:border-border/80 lg:pr-4">
            <div className="-mx-3 overflow-x-auto px-3 pb-1 sm:-mx-5 sm:px-5 lg:sticky lg:top-4 lg:mx-0 lg:overflow-visible lg:px-0 lg:pb-0">
              <div className="flex min-w-max items-start gap-1 lg:min-w-0 lg:flex-col lg:items-stretch lg:gap-4">
                {TAB_GROUPS.map((group) => (
                  <section key={group.key} className="contents lg:block lg:w-full">
                    <div className="hidden px-2.5 pb-1.5 text-[10px] font-semibold text-secondary/80 lg:block">
                      {group.label}
                    </div>
                    <div className="contents lg:block lg:space-y-1">
                      {visibleTabs.filter((tab) => tab.group === group.key).map(({ key, label, icon: Icon, badge }) => {
                        const isActive = !showingAccountPanel && activeTab.key === key
                        return (
                          <button
                            key={key}
                            type="button"
                            aria-current={isActive ? 'page' : undefined}
                            onClick={() => setSearchParams({ tab: key }, { replace: true })}
                            className={cn(
                              'group relative flex h-11 shrink-0 items-center gap-2.5 rounded-btn border px-2.5 text-left text-[13px] transition-colors duration-150 ease-smooth focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-2 focus-visible:ring-offset-base lg:w-full',
                              isActive
                                ? 'border-transparent text-foreground'
                                : 'border-transparent text-secondary hover:border-border/70 hover:bg-elevated/50 hover:text-foreground',
                            )}
                          >
                            {isActive && (
                              <>
                                <motion.span
                                  layoutId="settings-nav-active"
                                  className="absolute inset-0 rounded-btn bg-accent/[0.09]"
                                  transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                                />
                                <span className="absolute inset-y-2 left-0 hidden w-0.5 rounded-full bg-accent lg:block" />
                              </>
                            )}
                            <span
                              className={cn(
                                'relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors duration-150 ease-smooth',
                                isActive
                                  ? 'bg-accent/15 text-accent'
                                  : 'bg-elevated/80 text-secondary group-hover:bg-elevated-2 group-hover:text-foreground',
                              )}
                            >
                              <Icon className="h-4 w-4" />
                            </span>
                            <span className={cn('relative z-10 whitespace-nowrap', isActive && 'font-semibold')}>
                              {label}
                            </span>
                            {badge && (
                              <span className="relative z-10 ml-1 inline-flex shrink-0 items-center rounded border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[9px] font-semibold text-foreground/80 lg:ml-auto">
                                {badge}
                              </span>
                            )}
                          </button>
                        )
                      })}
                    </div>
                  </section>
                ))}
              </div>
            </div>
          </nav>

          {/* ===== Tab 内容 ===== */}
          <motion.div
            key={showingAccountPanel ? 'account' : activeTab.key}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.18 }}
            className="min-w-0 flex-1"
          >
            {showingAccountPanel
              ? <SettingsKeysPanel />
              : activeTab.key === 'monitoring'
                ? <SettingsMonitoringPanel highlight={highlight} />
                : <activeTab.panel />}
          </motion.div>
        </div>
      </div>
    </>
  )
}
