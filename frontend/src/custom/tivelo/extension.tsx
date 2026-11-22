import { lazy } from 'react'
import { Workflow } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

const Page = lazy(() => import('./Page').then((module) => ({ default: module.EngineResearch })))

const extension: FrontendExtension = {
  id: 'official.tivelo-research',
  apiVersion: 1,
  routes: [{ id: 'tivelo-research', path: '/research/engines', component: Page }],
  navigation: [{ id: 'tivelo-research', routeId: 'tivelo-research', label: '引擎研究', icon: Workflow }],
}

export default extension
