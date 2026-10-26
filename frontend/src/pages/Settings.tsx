/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { Suspense, lazy, useEffect, useState, type ComponentType } from 'react'
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Database, KeyRound, LayoutGrid, Library, Radio, Sparkles, Settings2, PanelLeftClose, PanelLeftOpen, Clock3, Info, HardDriveDownload } from 'lucide-react'
// 面板按 tab 按需加载: 8 个面板源码 270KB+, 同步打包会让设置页 chunk 膨胀
const SettingsAIPanel = lazy(() => import('./settings/AI').then(m => ({ default: m.SettingsAIPanel })))
const SettingsApiTokensPanel = lazy(() => import('./settings/ApiTokens').then(m => ({ default: m.SettingsApiTokensPanel })))
const SettingsBackupPanel = lazy(() => import('./settings/Backup').then(m => ({ default: m.SettingsBackupPanel })))
const SettingsDefinitionsPanel = lazy(() => import('./settings/Definitions').then(m => ({ default: m.SettingsDefinitionsPanel })))
const SettingsMonitoringPanel = lazy(() => import('./settings/Monitoring').then(m => ({ default: m.SettingsMonitoringPanel })))
const SettingsPageAdminPanel = lazy(() => import('./settings/PageAdmin').then(m => ({ default: m.SettingsPageAdminPanel })))
const SettingsTimeoutPanel = lazy(() => import('./settings/Timeout').then(m => ({ default: m.SettingsTimeoutPanel })))
const SettingsSystemPanel = lazy(() => import('./settings/System').then(m => ({ default: m.SettingsSystemPanel })))
const SettingsAboutPanel = lazy(() => import('./settings/About').then(m => ({ default: m.SettingsAboutPanel })))
const SettingsDataSourcesPanel = lazy(() => import('./settings/DataSources').then(m => ({ default: m.SettingsDataSourcesPanel })))
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'
import { setUiPref } from '@/lib/uiPrefs'

// ===== Tab 定义 =====

type TabDef = {
  key: string
  label: string
  icon: ComponentType<{ className?: string }>
  panel: ComponentType<{ highlight?: string }>
  badge?: string
}

const TABS: readonly TabDef[] = [
  { key: 'data-sources', label: '数据源',     icon: Database,  panel: SettingsDataSourcesPanel },
  { key: 'ai',         label: 'AI 设置',    icon: Sparkles,  panel: SettingsAIPanel },
  { key: 'monitoring', label: '实时监控',   icon: Radio,      panel: SettingsMonitoringPanel },
  { key: 'pages',      label: '页面管理',   icon: LayoutGrid, panel: SettingsPageAdminPanel },
  { key: 'timeout',    label: '网络设置',   icon: Clock3,     panel: SettingsTimeoutPanel },
  { key: 'api-tokens', label: '开放接口',   icon: KeyRound,   panel: SettingsApiTokensPanel },
  { key: 'definitions', label: '定义目录',   icon: Library,    panel: SettingsDefinitionsPanel },
  { key: 'backup',     label: '配置备份',   icon: HardDriveDownload, panel: SettingsBackupPanel },
  { key: 'system',     label: '系统设置',   icon: Settings2,  panel: SettingsSystemPanel },
  { key: 'about',      label: '关于',       icon: Info,       panel: SettingsAboutPanel },
]

// 「页面管理」归拢前的三个 tab key → 深链子区块, 历史链接零失效
const LEGACY_PAGE_TAB_SECTIONS: Record<string, string> = {
  menus: 'menus',
  'ext-pages': 'ext-pages',
  'page-packs': 'page-packs',
}

type TabKey = (typeof TABS)[number]['key']

export function Settings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') as TabKey | null
  // 旧 tab (menus/ext-pages/page-packs) 已并入「页面管理」: 渲染按 pages 处理,
  // 再用 replace 把 URL 归一化并透传 section, 保持深链与历史记录干净
  const legacySection = tabParam ? LEGACY_PAGE_TAB_SECTIONS[tabParam] : undefined
  const activeTab = TABS.find((t) => t.key === (legacySection ? 'pages' : tabParam)) ?? TABS[0]
  const highlight = searchParams.get('highlight') ?? ''

  useEffect(() => {
    if (!legacySection) return
    const next = new URLSearchParams(searchParams)
    next.set('tab', 'pages')
    next.set('section', legacySection)
    setSearchParams(next, { replace: true })
  }, [legacySection, searchParams, setSearchParams])

  // 设置菜单收起状态 — 持久化到 localStorage
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('tf-settings-nav-collapsed') === '1' } catch { return false }
  })
  const toggleCollapsed = () => {
    setCollapsed(prev => {
      const next = !prev
      try { setUiPref('tf-settings-nav-collapsed', next ? '1' : '0') } catch {}
      return next
    })
  }

  return (
    <>
      <PageHeader
        title="设置"
        subtitle="管理账户、数据刷新策略和高级功能配置。"
      />

      <div className="px-8 py-6">
        <div className="flex gap-6 items-stretch">
          {/* ===== 竖向 Tab 侧栏 ===== */}
          <nav className={cn('shrink-0 transition-all duration-200 ease-smooth', collapsed ? 'w-10' : 'w-36')}>
            <div className="flex flex-col gap-0.5 justify-center min-h-[60vh] sticky top-6">
              {/* 收起/展开 按钮 */}
              <button
                onClick={toggleCollapsed}
                className={cn(
                  'flex items-center gap-2 rounded-btn text-muted hover:text-foreground hover:bg-elevated/60 transition-colors duration-150 ease-smooth mb-1',
                  collapsed ? 'justify-center px-0 py-2' : 'px-3 py-2 text-xs',
                )}
                title={collapsed ? '展开菜单' : '收起菜单'}
              >
                {collapsed
                  ? <PanelLeftOpen className="h-3.5 w-3.5 shrink-0" />
                  : <PanelLeftClose className="h-3.5 w-3.5 shrink-0" />
                }
                {!collapsed && <span>收起菜单</span>}
              </button>

              {/* Tab 按钮列表 — 收起时只显示图标 */}
              {TABS.map(({ key, label, icon: Icon, badge }) => (
                <button
                  key={key}
                  onClick={() => setSearchParams({ tab: key }, { replace: true })}
                  title={collapsed ? label : undefined}
                  className={cn(
                    'relative flex items-center rounded-btn text-sm transition-colors duration-150 ease-smooth',
                    collapsed ? 'justify-center px-0 py-2' : 'items-center gap-2 px-3 py-2 text-left',
                    activeTab.key === key
                      ? 'bg-accent/10 text-accent font-medium'
                      : 'text-secondary hover:text-foreground hover:bg-elevated/60',
                  )}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  {!collapsed && <span>{label}</span>}
                  {!collapsed && badge && (
                    <span className="ml-auto inline-flex items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400 shrink-0">
                      {badge}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </nav>

          {/* ===== Tab 内容 ===== */}
          <motion.div
            key={activeTab.key}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="min-w-0 flex-1"
          >
            <Suspense fallback={<div className="py-12 text-center text-xs text-muted">加载中…</div>}>
              {activeTab.key === 'monitoring'
                ? <SettingsMonitoringPanel highlight={highlight} />
                : <activeTab.panel highlight={highlight} />}
            </Suspense>
          </motion.div>
        </div>
      </div>
    </>
  )
}
