import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconPaper } from '@/components/BrandIcons'

export const Paper = lazy(() => import('./Page').then(m => ({ default: m.Paper })))

const extension: FrontendExtension = {
  id: 'official.paper',
  apiVersion: 1,
  routes: [
    { id: 'paper', path: '/paper', component: Paper },
  ],
  navigation: [
    { id: 'paper', routeId: 'paper', label: '模拟盘', icon: IconPaper },
  ],
}

export default extension
