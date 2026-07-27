import { lazy } from 'react'
import { NotebookPen, WalletCards } from 'lucide-react'
import type { RouteObject } from 'react-router-dom'

import type { NavItem } from '@/lib/navRegistry'

const ResearchLedger = lazy(() => import('./research-ledger/ResearchLedgerPage').then(module => ({
  default: module.ResearchLedgerPage,
})))
const Portfolio = lazy(() => import('./portfolio/PortfolioPage').then(module => ({
  default: module.PortfolioPage,
})))

/** Frozen frontend gateway: future Sycee pages register here, not in the upstream router. */
export const SYCEE_ROUTES: RouteObject[] = [
  { path: 'portfolio', element: <Portfolio /> },
  { path: 'research-ledger', element: <ResearchLedger /> },
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
    id: '/research-ledger',
    to: '/research-ledger',
    label: '研究账本',
    icon: NotebookPen,
    group: 'strategy',
    extension: false,
  },
]
