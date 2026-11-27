import type { FrontendExtension } from '@/extensions/types'
import { AuctionScanCard } from './AuctionScanCard'

const extension: FrontendExtension = {
  id: 'official.auction-scan',
  apiVersion: 1,
  slots: [
    {
      name: 'abnormal.auction.extra',
      id: 'auction-scan-card',
      order: 10,
      component: AuctionScanCard,
    },
  ],
}

export default extension
