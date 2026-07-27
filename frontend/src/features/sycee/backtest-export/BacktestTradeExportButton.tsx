import { Download } from 'lucide-react'

import { toast } from '@/components/Toast'
import type { StrategyBacktestResult } from '@/lib/api'
import { buildBacktestTradeExportFilename, buildBacktestTradesCsv } from './backtestTradeCsv'

interface Props {
  result: StrategyBacktestResult
}

export function BacktestTradeExportButton({ result }: Props) {
  const tradeCount = result.trades.length
  const title = tradeCount > 0 ? `导出全部 ${tradeCount} 条交易明细为 CSV` : '暂无交易明细可导出'

  const download = () => {
    try {
      const blob = new Blob([buildBacktestTradesCsv(result)], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = buildBacktestTradeExportFilename(result)
      anchor.style.display = 'none'
      document.body.append(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
      toast(`已导出 ${tradeCount} 条交易明细`, 'success')
    } catch {
      toast('导出交易明细失败', 'error')
    }
  }

  return (
    <button
      type="button"
      onClick={download}
      disabled={tradeCount === 0}
      title={title}
      aria-label={title}
      className="col-span-3 mb-2 ml-auto mr-1 inline-flex h-8 items-center justify-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-[11px] font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-45 sm:col-auto sm:mb-1.5 sm:mr-0 sm:h-7"
    >
      <Download className="h-3.5 w-3.5" />
      导出 CSV
    </button>
  )
}
