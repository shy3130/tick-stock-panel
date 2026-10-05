import { Suspense, lazy, useEffect, useState } from 'react'
import type { Props } from './StockPreviewDialogContent'

// 懒加载壳: 关闭 (symbol=null) 时不挂载内容 → StockPanel→echarts 整条链不进任何
// 列表页/落地页首屏; 关闭后保留 300ms 让 Modal 退出动画播完再卸载。
// 卸载同时清空弹窗内临时状态 (视图/日期范围), 与「弹窗关闭即重置」规范一致。
const StockPreviewDialogContent = lazy(() =>
  import('./StockPreviewDialogContent').then(m => ({ default: m.StockPreviewDialogContent })),
)

export function StockPreviewDialog(props: Props) {
  const [mounted, setMounted] = useState(!!props.symbol)
  useEffect(() => {
    if (props.symbol) {
      setMounted(true)
      return
    }
    const t = setTimeout(() => setMounted(false), 300)
    return () => clearTimeout(t)
  }, [props.symbol])
  if (!mounted) return null
  return (
    <Suspense fallback={null}>
      <StockPreviewDialogContent {...props} />
    </Suspense>
  )
}
