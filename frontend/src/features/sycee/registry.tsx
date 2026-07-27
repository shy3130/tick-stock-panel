import { lazy } from 'react'
import { Activity, DatabaseBackup, GitCompareArrows, Newspaper, NotebookPen, WalletCards } from 'lucide-react'
import type { RouteObject } from 'react-router-dom'

import type { NavItem } from '@/lib/navRegistry'

const ResearchLedger = lazy(() => import('./research-ledger/ResearchLedgerPage').then(module => ({
  default: module.ResearchLedgerPage,
})))
const Portfolio = lazy(() => import('./portfolio/PortfolioPage').then(module => ({
  default: module.PortfolioPage,
})))
const StrategyCompare = lazy(() => import('./strategy-compare/StrategyComparePage').then(module => ({
  default: module.StrategyComparePage,
})))
const DataBackup = lazy(() => import('./data-backup/DataBackupPage').then(module => ({
  default: module.DataBackupPage,
})))
const StrategyTracking = lazy(() => import('./strategy-tracking/StrategyTrackingPage').then(module => ({
  default: module.StrategyTrackingPage,
})))
const DailyBriefing = lazy(() => import('./daily-briefing/DailyBriefingPage').then(module => ({
  default: module.DailyBriefingPage,
})))

/** Frozen frontend gateway: future Sycee pages register here, not in the upstream router. */
export const SYCEE_ROUTES: RouteObject[] = [
  { path: 'portfolio', element: <Portfolio /> },
  { path: 'daily-briefing', element: <DailyBriefing /> },
  { path: 'strategy-compare', element: <StrategyCompare /> },
  { path: 'research-ledger', element: <ResearchLedger /> },
  { path: 'strategy-tracking', element: <StrategyTracking /> },
  { path: 'data-backup', element: <DataBackup /> },
]

/** Frozen navigation gateway: future Sycee entries register here, not in navRegistry. */
export const SYCEE_NAV_ITEMS: NavItem[] = [
  {
    id: '/portfolio',
    to: '/portfolio',
    label: '持仓',
    icon: WalletCards,
    group: 'core',
    extension: false,
  },
  {
    id: '/daily-briefing',
    to: '/daily-briefing',
    label: '每日简报',
    icon: Newspaper,
    group: 'core',
    extension: false,
  },
  {
    id: '/strategy-compare',
    to: '/strategy-compare',
    label: '策略对比',
    icon: GitCompareArrows,
    group: 'strategy',
    extension: false,
  },
  {
    id: '/strategy-tracking',
    to: '/strategy-tracking',
    label: '策略跟踪',
    icon: Activity,
    group: 'strategy',
    extension: false,
  },
  {
    id: '/research-ledger',
    to: '/research-ledger',
    label: '研究账本',
    icon: NotebookPen,
    group: 'strategy',
    extension: false,
  },
  {
    id: '/data-backup',
    to: '/data-backup',
    label: '数据备份',
    icon: DatabaseBackup,
    group: 'system',
    extension: false,
  },
]
