import { useRef, useCallback } from 'react'

/**
 * 对话框遮罩"点击外部关闭"的共享逻辑, 防止拖拽穿透。

 * 问题: 用户在对话框内容内按下鼠标拖选文本, 鼠标移到遮罩上松开时,
 * 浏览器仍会触发遮罩的 click 事件 (click 派发给 mousedown/mouseup 的共同祖先),
 * 导致对话框意外关闭。

 * 解法: 记录 mousedown 时是否落在遮罩本身上; 仅当 mousedown 和 mouseup(click)
 * 都发生在遮罩上时才触发关闭。

 * 用法:
 *   const backdrop = useDialogBackdrop(onClose)
 *   <div className="fixed inset-0 ..." {...backdrop}>
 *     <div onClick={e => e.stopPropagation()}>内容</div>
 *   </div>
 *
 * 对于有额外条件(如 isWorking 时禁止关闭)的场景, 传 enabled 回调:
 *   const backdrop = useDialogBackdrop(onClose, () => !isWorking)
 */
export function useDialogBackdrop(
  onClose: () => void,
  enabled?: () => boolean,
) {
  const mouseDownOnBackdrop = useRef(false)

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    mouseDownOnBackdrop.current = e.target === e.currentTarget
  }, [])

  const onClick = useCallback((e: React.MouseEvent) => {
    if (enabled && !enabled()) return
    if (mouseDownOnBackdrop.current && e.target === e.currentTarget) {
      onClose()
    }
  }, [onClose, enabled])

  return { onMouseDown, onClick }
}
