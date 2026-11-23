import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconRegime } from '@/components/BrandIcons'

const Regime = lazy(() => import('./Page').then(module => ({ default: module.Regime })))

const extension: FrontendExtension = {
  id: 'official.market-regime',
  apiVersion: 1,
  routes: [{ id: 'regime', path: '/regime', component: Regime }],
  navigation: [{ id: 'regime', routeId: 'regime', label: '市场环境', icon: IconRegime }],
}

export default extension
