import type { ReactNode } from 'react'
import { BarChart3, Settings, Tags } from 'lucide-react'
import {
  IconDashboard, IconWatchlist, IconStrategy, IconFactors, IconBacktest,
  IconRegime, IconIndices, IconData, type BrandIconProps,
} from '@/components/BrandIcons'
import type { AnalysisMenu } from './api'
import { getFrontendExtensionNavigation } from '@/extensions/registry'

export type NavigationEntry = {
  id: string; to: string; label: string; icon: (props: BrandIconProps) => ReactNode
  type: 'builtin' | 'analysis' | 'extension'
  visible: boolean; arch?: 'core' | 'ext'; badge?: string; extensionId?: string
}

export const CORE_NAVIGATION: NavigationEntry[] = [
  { id: '/', label: '看板', icon: IconDashboard },
  { id: '/watchlist', label: '自选', icon: IconWatchlist },
  { id: '/screener', label: '策略', icon: IconStrategy },
  { id: '/factors', label: '因子', icon: IconFactors },
  { id: '/backtest', label: '回测', icon: IconBacktest },
  { id: '/regime', label: '市场环境', icon: IconRegime },
  { id: '/indices', label: '指数', icon: IconIndices },
  { id: '/data', label: '数据', icon: IconData },
  { id: '/settings', label: '设置', icon: Settings },
].map(x => ({ ...x, to: x.id, type: 'builtin', visible: true, arch: 'core' }))

/** 宿主与管理页共用来源及 ID，分析页历史裸 ID 只在兼容读取时转换。 */
export function getNavigationEntries(menus: readonly AnalysisMenu[] = []): NavigationEntry[] {
  const entries: NavigationEntry[] = [
    ...CORE_NAVIGATION,
    ...menus.map(m => ({
      id: `/analysis/${m.id}`, to: `/analysis/${m.id}`, label: m.label,
      type: 'analysis' as const, visible: m.visible,
      icon: m.icon === 'tags' ? Tags : BarChart3, arch: 'ext' as const,
    })),
    ...getFrontendExtensionNavigation().map(item => ({
      id: item.route.path, to: item.route.path, label: item.label, icon: item.icon,
      type: 'extension' as const, visible: true, arch: 'ext' as const,
      badge: item.badge, extensionId: item.extensionId,
    })),
  ]
  return [...new Map(entries.map(x => [x.id, x])).values()]
}
