/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { BarChart3, Database, Key, LockKeyhole, Radio, SlidersHorizontal, Sparkles, Settings2, Zap } from 'lucide-react'
import { SettingsKeysPanel } from './settings/Keys'
import { SettingsAIPanel } from './settings/AI'
import { SettingsMonitoringPanel } from './settings/Monitoring'
import { SettingsExtPagesPanel } from './settings/ExtPages'
import { SettingsMenuSettingsPanel } from './settings/MenuSettings'
import { SettingsSystemPanel } from './settings/System'
import { SettingsCustomSignalsPanel } from './settings/CustomSignals'
import { SettingsDataSourcesPanel } from './settings/DataSources'
import { SettingsAccountSecurityPanel } from './settings/AccountSecurity'
import { SettingsTabs, type SettingsTabItem } from './settings/SettingsTabs'
import { PageHeader } from '@/components/PageHeader'

import type { ComponentType } from 'react'

// ===== Tab 定义 =====

type TabDef = SettingsTabItem & {
  panel: ComponentType<{ highlight?: string }>
}

const TABS: readonly TabDef[] = [
  { key: 'account',    label: 'TickFlow',   icon: Key,       panel: SettingsKeysPanel },
  { key: 'security',   label: '账户安全',     icon: LockKeyhole, panel: SettingsAccountSecurityPanel },
  { key: 'ai',         label: 'AI 设置',    icon: Sparkles,  panel: SettingsAIPanel },
  { key: 'monitoring', label: '实时监控',   icon: Radio,     panel: SettingsMonitoringPanel },
  { key: 'data-sources', label: '数据源',     icon: Database,  panel: SettingsDataSourcesPanel, badge: 'beta' },
  { key: 'ext-pages',  label: '扩展页面',   icon: BarChart3, panel: SettingsExtPagesPanel },
  { key: 'signals',    label: '信号库',     icon: Zap,       panel: SettingsCustomSignalsPanel },
  { key: 'menus',      label: '菜单设置',   icon: SlidersHorizontal, panel: SettingsMenuSettingsPanel },
  { key: 'system',     label: '系统设置',   icon: Settings2, panel: SettingsSystemPanel },
]

type TabKey = (typeof TABS)[number]['key']

export function Settings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab') as TabKey | null
  const activeTab = TABS.find((t) => t.key === tabParam) ?? TABS[0]
  const highlight = searchParams.get('highlight') ?? ''

  return (
    <>
      <PageHeader
        title="设置"
        subtitle="管理账户、数据刷新策略和高级功能配置。"
      />

      <div className="px-3 py-4 sm:px-5 lg:px-8 lg:py-6">
        <div className="flex flex-col gap-4 md:flex-row md:items-stretch md:gap-6">
          {/* ===== 竖向 Tab 侧栏（内容垂直居中） ===== */}
          <SettingsTabs
            tabs={TABS}
            activeKey={activeTab.key}
            onChange={(key) => setSearchParams({ tab: key }, { replace: true })}
          />

          {/* ===== Tab 内容 ===== */}
          <motion.div
            key={activeTab.key}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.15 }}
            className="w-full min-w-0 flex-1"
          >
            {activeTab.key === 'monitoring'
            ? <SettingsMonitoringPanel highlight={highlight} />
            : <activeTab.panel />}
          </motion.div>
        </div>
      </div>
    </>
  )
}
