/**
 * 统一设置页面 — Tab 切换外壳。
 *
 * 通过 URL query param ?tab=xxx 同步 Tab 状态。
 */
import { useSearchParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { BarChart3, Key, Lightbulb, Radio, SlidersHorizontal, Sparkles, Settings2, Zap } from 'lucide-react'
import { SettingsKeysPanel } from './settings/Keys'
import { SettingsAIPanel } from './settings/AI'
import { SettingsMonitoringPanel } from './settings/Monitoring'
import { SettingsExtPagesPanel } from './settings/ExtPages'
import { SettingsMenuSettingsPanel } from './settings/MenuSettings'
import { SettingsSystemPanel } from './settings/System'
import { SettingsCustomSignalsPanel } from './settings/CustomSignals'
import { SettingsProposalsPanel } from './settings/Proposals'
import { PageHeader } from '@/components/PageHeader'
import { cn } from '@/lib/cn'

// ===== Tab 定义 =====

const TABS = [
  { key: 'account',    label: '数据源',     icon: Key,       panel: SettingsKeysPanel },
  { key: 'ai',         label: 'AI 设置',    icon: Sparkles,  panel: SettingsAIPanel },
  { key: 'monitoring', label: '实时监控',   icon: Radio,     panel: SettingsMonitoringPanel },
  { key: 'ext-pages',  label: '扩展页面',   icon: BarChart3, panel: SettingsExtPagesPanel },
  { key: 'signals',    label: '信号库',     icon: Zap,       panel: SettingsCustomSignalsPanel },
  { key: 'proposals',  label: '策略提案',   icon: Lightbulb, panel: SettingsProposalsPanel },
  { key: 'menus',      label: '菜单设置',   icon: SlidersHorizontal, panel: SettingsMenuSettingsPanel },
  { key: 'system',     label: '系统设置',   icon: Settings2, panel: SettingsSystemPanel },
] as const

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

      <div className="px-8 py-6">
        <div className="flex gap-6 items-stretch">
          {/* ===== 竖向 Tab 侧栏（内容垂直居中） ===== */}
          <nav className="w-36 shrink-0">
            <div className="flex flex-col gap-0.5 justify-center min-h-[60vh] sticky top-6">
              {TABS.map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  onClick={() => setSearchParams({ tab: key }, { replace: true })}
                  className={cn(
                    'relative flex items-center gap-2 px-3 py-2 rounded-btn text-sm transition-colors duration-150 ease-smooth text-left',
                    activeTab.key === key
                      ? 'bg-accent/10 text-accent font-medium'
                      : 'text-secondary hover:text-foreground hover:bg-elevated/60',
                  )}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                  <span>{label}</span>
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
            {activeTab.key === 'monitoring'
            ? <SettingsMonitoringPanel highlight={highlight} />
            : <activeTab.panel />}
          </motion.div>
        </div>
      </div>
    </>
  )
}
