import { useDialogTask, useDialogState } from '@/lib/aiReportStore'
import { AiAnalysisDialog } from './AiAnalysisDialog'

/**
 * AI 分析对话框宿主 —— 单点挂载在 Layout。
 *
 * 从 store 读取当前对话框状态(任务 + 最小化),把 AiAnalysisDialog 作为纯视图渲染。
 * 一次挂载,全局生效:任意页面发起的分析都会显示在这个对话框里。
 */
export function AiAnalysisHost() {
  const { task, mode } = useDialogTask()
  const { minimized } = useDialogState()
  return <AiAnalysisDialog task={task} mode={mode} minimized={minimized} />
}
