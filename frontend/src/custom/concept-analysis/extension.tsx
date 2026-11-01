import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconConcept } from '@/components/BrandIcons'

export const ConceptAnalysis = lazy(() => import('./Page').then(m => ({ default: m.ConceptAnalysis })))

const extension: FrontendExtension = {
  id: 'official.concept-analysis',
  apiVersion: 1,
  routes: [
    { id: 'concept-analysis', path: '/concept-analysis', component: ConceptAnalysis },
  ],
  navigation: [
    { id: 'concept-analysis', routeId: 'concept-analysis', label: '概念板块', icon: IconConcept },
  ],
}

export default extension
