import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconAlert } from '@/components/BrandIcons'

export const AbnormalMoves = lazy(() => import('./Page').then(m => ({ default: m.AbnormalMoves })))

const extension: FrontendExtension = {
  id: 'official.abnormal',
  apiVersion: 1,
  routes: [
    { id: 'abnormal', path: '/abnormal', component: AbnormalMoves },
  ],
  navigation: [
    { id: 'abnormal', routeId: 'abnormal', label: '异动监控', icon: IconAlert },
  ],
}

export default extension
