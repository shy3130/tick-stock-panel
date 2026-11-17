import { lazy } from 'react'
import { Wallet } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
const Page = lazy(() => import('./Page'))
const extension: FrontendExtension = {
  id: 'personal.holdings', apiVersion: 1,
  routes: [{ id: 'holdings', path: '/holdings', component: Page }],
  navigation: [{ id: 'holdings', routeId: 'holdings', label: '持仓管理', icon: Wallet }],
}
export default extension
