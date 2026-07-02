import { useEffect } from 'react'
import { ToastContainer, toast } from 'tickflow-stock-panel-frontend'

export function ErrorAndSuccess() {
  useEffect(() => {
    toast('保存失败,请检查网络连接', 'error')
    toast('策略已保存', 'success')
  }, [])
  return (
    <div style={{ position: 'relative', height: 160 }}>
      <ToastContainer />
    </div>
  )
}
