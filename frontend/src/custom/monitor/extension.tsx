import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconMonitor } from '@/components/BrandIcons'

export const Monitor = lazy(() => import('./Page').then(m => ({ default: m.Monitor })))

const extension: FrontendExtension = {
  id: 'official.monitor',
  apiVersion: 1,
  routes: [
    { id: 'monitor', path: '/monitor', component: Monitor },
  ],
  navigation: [
    { id: 'monitor', routeId: 'monitor', label: '监控中心', icon: IconMonitor },
  ],
}

export default extension
