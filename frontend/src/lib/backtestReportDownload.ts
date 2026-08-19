import type { BacktestRun } from './api.ts'
import { buildRunReportHtml } from './backtestReport.ts'

/** 稳定且安全的离线报告文件名：`backtest-report-{runId前8}-{YYYYMMDD}.html` */
export function buildRunReportFilename(runId: string, now: Date = new Date()): string {
  const idPart = String(runId ?? '')
    .slice(0, 8)
    .replace(/[^a-zA-Z0-9_-]/g, '') || 'run'
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `backtest-report-${idPart}-${y}${m}${d}.html`
}

/**
 * 将完整 BacktestRun 生成为自包含 HTML，并触发本地下载。
 * - 仅 Blob + object URL + 临时 <a>，无后端端点、无外链、无 window.open
 * - 下载触发后立即 revoke object URL
 */
export function downloadRunReportHtml(run: BacktestRun): void {
  const html = buildRunReportHtml(run)
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = buildRunReportFilename(run.run_id)
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}
