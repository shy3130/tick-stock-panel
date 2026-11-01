import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconSignals } from '@/components/BrandIcons'

export const Signals = lazy(() => import('./Page').then(m => ({ default: m.Signals })))

const extension: FrontendExtension = {
  id: 'official.signals',
  apiVersion: 1,
  routes: [
    { id: 'signals', path: '/signals', component: Signals },
  ],
  navigation: [
    { id: 'signals', routeId: 'signals', label: '信号库', icon: IconSignals },
  ],
}

export default extension
