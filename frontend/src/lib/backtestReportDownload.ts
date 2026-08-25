import type { BacktestRun, BacktestRunComparison } from './api.ts'
import { buildRunReportHtml } from './backtestReport.ts'
import { buildCompareReportHtml } from './compareReport.ts'

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

/** 对比报告文件名：`回测对比_YYYYMMDD_HHmm.html` */
export function buildCompareReportFilename(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const y = now.getFullYear()
  const m = pad(now.getMonth() + 1)
  const d = pad(now.getDate())
  const hh = pad(now.getHours())
  const mm = pad(now.getMinutes())
  return `回测对比_${y}${m}${d}_${hh}${mm}.html`
}

/** Blob → object URL → 临时 <a> 触发本地下载，结束后立即 revoke object URL */
function triggerHtmlDownload(html: string, filename: string): void {
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

/**
 * 将完整 BacktestRun 生成为自包含 HTML，并触发本地下载。
 * - 仅 Blob + object URL + 临时 <a>，无后端端点、无外链、无 window.open
 * - 下载触发后立即 revoke object URL
 */
export function downloadRunReportHtml(run: BacktestRun): void {
  triggerHtmlDownload(buildRunReportHtml(run), buildRunReportFilename(run.run_id))
}

/** 将 Run 对比结果生成为自包含 HTML 对比报告，并触发本地下载（无后端端点、无外链）。 */
export function downloadCompareReportHtml(comparison: BacktestRunComparison, now: Date = new Date()): void {
  triggerHtmlDownload(buildCompareReportHtml(comparison, now), buildCompareReportFilename(now))
}
