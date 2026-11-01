import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconReview } from '@/components/BrandIcons'

export const ReviewPage = lazy(() => import('./Page').then(m => ({ default: m.ReviewPage })))

export const DragonTigerCard = lazy(() => import('./Page').then(m => ({ default: m.DragonTigerCard })))

const extension: FrontendExtension = {
  id: 'official.review',
  apiVersion: 1,
  routes: [
    { id: 'review', path: '/review', component: ReviewPage },
  ],
  navigation: [
    { id: 'review', routeId: 'review', label: '复盘', icon: IconReview },
  ],
}

export default extension
