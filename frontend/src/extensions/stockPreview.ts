export interface StockPreviewItem {
  /** 完整代码，包含交易所后缀；不根据数字代码猜市场或资产类型。 */
  symbol: string
  name?: string | null
}

export interface OpenStockPreviewOptions extends StockPreviewItem {
  /** 当前标的在此列表中且至少有两项时，启用前后切股。 */
  navList?: readonly StockPreviewItem[]
}

export interface StockPreviewUi {
  /** 接受有效请求返回 true，参数无效返回 false，不替换已打开的详情。 */
  openStockPreview: (options: OpenStockPreviewOptions) => boolean
  closeStockPreview: () => void
}

function hostUi(): StockPreviewUi | undefined {
  return typeof window === 'undefined' ? undefined
    : (window as unknown as { __TSP_EXT__?: { ui?: StockPreviewUi } }).__TSP_EXT__?.ui
}

/** 始终调用宿主，源码被独立编译为页面包后也不会复制一套无宿主的弹窗状态。 */
export function openStockPreview(options: OpenStockPreviewOptions): boolean {
  const open = hostUi()?.openStockPreview
  return typeof open === 'function' ? open(options) : false
}

export function closeStockPreview(): void {
  const close = hostUi()?.closeStockPreview
  if (typeof close === 'function') close()
}
