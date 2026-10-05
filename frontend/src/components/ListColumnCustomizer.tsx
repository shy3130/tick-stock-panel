import { Suspense, lazy, useEffect, useState } from 'react'
import type { ListColumnCustomizerProps } from './ListColumnCustomizerInner'

// 懒加载壳: 关闭时不挂载内容 → @dnd-kit 不随列表页首屏加载; 关闭后保留 300ms
// 让内容 AnimatePresence 的退出动画播完再卸载。
const ListColumnCustomizerInner = lazy(() =>
  import('./ListColumnCustomizerInner').then(m => ({ default: m.ListColumnCustomizerInner })),
)

export function ListColumnCustomizer(props: ListColumnCustomizerProps) {
  const [mounted, setMounted] = useState(!!props.open)
  useEffect(() => {
    if (props.open) {
      setMounted(true)
      return
    }
    const t = setTimeout(() => setMounted(false), 300)
    return () => clearTimeout(t)
  }, [props.open])
  if (!mounted) return null
  return (
    <Suspense fallback={null}>
      <ListColumnCustomizerInner {...props} />
    </Suspense>
  )
}
