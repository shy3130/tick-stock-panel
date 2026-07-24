import { useDialogTask, useDialogState } from '@/lib/stockAnalysisStore'
import { StockAnalysisDialog } from './StockAnalysisDialog'

/**
 * AI 个股分析对话框宿主 —— 单点挂载在 Layout。
 * 与财务分析的 AiAnalysisHost 并列,独立 store,蓝色主题。
 */
export function StockAnalysisHost() {
  const { task, mode } = useDialogTask()
  const { minimized } = useDialogState()
  return <StockAnalysisDialog task={task} mode={mode} minimized={minimized} />
}
