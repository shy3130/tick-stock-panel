import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconIndustry } from '@/components/BrandIcons'

export const IndustryAnalysis = lazy(() => import('./Page').then(m => ({ default: m.IndustryAnalysis })))

const extension: FrontendExtension = {
  id: 'official.industry-analysis',
  apiVersion: 1,
  routes: [
    { id: 'industry-analysis', path: '/industry-analysis', component: IndustryAnalysis },
  ],
  navigation: [
    { id: 'industry-analysis', routeId: 'industry-analysis', label: '行业板块', icon: IconIndustry },
  ],
}

export default extension
