import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconStockFocus } from '@/components/BrandIcons'

export const StockAnalysis = lazy(() => import('./Page').then(m => ({ default: m.StockAnalysis })))

const extension: FrontendExtension = {
  id: 'official.stock-analysis',
  apiVersion: 1,
  routes: [
    { id: 'stock-analysis', path: '/stock-analysis', component: StockAnalysis },
  ],
  navigation: [
    { id: 'stock-analysis', routeId: 'stock-analysis', label: '个股分析', icon: IconStockFocus },
  ],
}

export default extension
