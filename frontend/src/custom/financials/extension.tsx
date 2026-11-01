import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconFinancials } from '@/components/BrandIcons'

export const Financials = lazy(() => import('./Page').then(m => ({ default: m.Financials })))

const extension: FrontendExtension = {
  id: 'official.financials',
  apiVersion: 1,
  routes: [
    { id: 'financials', path: '/financials', component: Financials },
  ],
  navigation: [
    { id: 'financials', routeId: 'financials', label: '财务概况', icon: IconFinancials },
  ],
}

export default extension
