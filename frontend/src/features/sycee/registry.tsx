import { lazy } from 'react'
import { NotebookPen } from 'lucide-react'
import type { RouteObject } from 'react-router-dom'

import type { NavItem } from '@/lib/navRegistry'

const ResearchLedger = lazy(() => import('./research-ledger/ResearchLedgerPage').then(module => ({
  default: module.ResearchLedgerPage,
})))

/** Frozen frontend gateway: future Sycee pages register here, not in the upstream router. */
export const SYCEE_ROUTES: RouteObject[] = [
  { path: 'research-ledger', element: <ResearchLedger /> },
]

/** Frozen navigation gateway: future Sycee entries register here, not in navRegistry. */
export const SYCEE_NAV_ITEMS: NavItem[] = [
  {
    id: '/research-ledger',
    to: '/research-ledger',
    label: '研究账本',
    icon: NotebookPen,
    group: 'strategy',
    extension: false,
  },
]
