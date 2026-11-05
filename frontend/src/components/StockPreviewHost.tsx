import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { useLocation } from 'react-router-dom'
import {
  stockPreviewUi, getStockPreviewRequest, navigateStockPreview, subscribeStockPreview,
} from '@/lib/stockPreviewStore'
import { StockPreviewDialog } from './StockPreviewDialog'

const { closeStockPreview } = stockPreviewUi

/** 扩展调用的唯一详情宿主，沿用原弹窗的懒加载、查询缓存和关闭清理。 */
export function StockPreviewHost() {
  const request = useSyncExternalStore(subscribeStockPreview, getStockPreviewRequest, getStockPreviewRequest)
  const location = useLocation()
  const previousLocation = useRef(location.key)
  useLayoutEffect(() => {
    if (previousLocation.current !== location.key) {
      previousLocation.current = location.key
      // 新页面的挂载回调可能已打开另一只标的，不能误关它。
      if (getStockPreviewRequest() === request) closeStockPreview()
    }
  }, [location.key, request])
  useEffect(() => () => closeStockPreview(), [])
  return <StockPreviewDialog symbol={request?.symbol ?? null} name={request?.name} navList={request?.navList}
    onClose={closeStockPreview} onNavigate={navigateStockPreview} />
}
