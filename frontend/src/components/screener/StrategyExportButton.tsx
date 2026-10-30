import { useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'
import { downloadResourceBundle } from '@/lib/resourceTransfer'

/** 设置弹窗导出已保存策略及其依赖包。 */
export function StrategyExportButton({ strategyId, disabled = false, disabledHint, className }: {
  strategyId: string; disabled?: boolean; disabledHint?: string; className?: string
}) {
  const [busy, setBusy] = useState(false)
  const download = async () => {
    if (busy || disabled) return
    setBusy(true)
    try {
      await downloadResourceBundle([{ kind: 'strategy', id: strategyId }], `tsp-strategy-${strategyId}.zip`)
    } catch (error) {
      toast(error instanceof Error ? error.message : '策略包导出失败，请重试', 'error')
    } finally { setBusy(false) }
  }
  return <button type="button" aria-label="导出策略包" disabled={disabled || busy}
    title={disabled && disabledHint ? disabledHint : '导出 ZIP 策略包，包含相关因子、信号与已保存调参'}
    onClick={event => { event.stopPropagation(); void download() }}
    className={cn('inline-flex shrink-0 items-center justify-center rounded p-0.5 text-muted transition-colors hover:bg-elevated hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40', className)}>
    {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
  </button>
}
