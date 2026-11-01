import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconLadder } from '@/components/BrandIcons'

export const LimitUpLadder = lazy(() => import('./Page').then(m => ({ default: m.LimitUpLadder })))

const extension: FrontendExtension = {
  id: 'official.limit-ladder',
  apiVersion: 1,
  routes: [
    { id: 'limit-ladder', path: '/limit-ladder', component: LimitUpLadder },
  ],
  navigation: [
    { id: 'limit-ladder', routeId: 'limit-ladder', label: '连板梯队', icon: IconLadder },
  ],
}

export default extension
