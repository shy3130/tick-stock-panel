import { useState } from 'react'
import { BookmarkPlus, Eye, Filter, LineChart, Loader2, ShieldCheck } from 'lucide-react'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toast } from '@/components/Toast'
import { type ShortPoolCard } from '@/lib/shortPoolCard'
import { stageScreenerBacktestHandoff } from '@/lib/screenerBacktestHandoff'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { useNavigate } from 'react-router-dom'

interface ShortPoolPanelProps {
  card: ShortPoolCard
}

export function ShortPoolPanel({ card }: ShortPoolPanelProps) {
  const navigate = useNavigate()
  const batchAdd = useWatchlistBatchAdd()
  const [preview, setPreview] = useState<{ symbol: string; name: string } | null>(null)
  const symbols = card.candidates.map(candidate => candidate.symbol)
  const hasCandidates = symbols.length > 0

  const handleBatchAdd = () => {
    if (!hasCandidates) return
    batchAdd.mutate(symbols, {
      onSuccess: (data) => toast(`已添加 ${data.added} 只到自选`, 'success'),
      onError: () => toast('添加自选失败', 'error'),
    })
  }

  const handleBacktest = () => {
    if (!hasCandidates) return
    if (stageScreenerBacktestHandoff({ target: 'strategy', symbols, asOf: card.as_of }) === 0) {
      toast('当前观察池没有可带入回测的标的代码', 'error')
      return
    }
    navigate('/backtest')
  }

  return (
    <section className="mt-2 overflow-hidden rounded-input border border-border bg-elevated/60" aria-label="AI 短线池">
      <header className="border-b border-border/80 px-2.5 py-2">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-muted">
          <span className="inline-flex items-center gap-1 font-medium text-secondary">
            <Filter className="h-3 w-3 text-accent" />
            短线动量质量观察
          </span>
          <span className="font-mono">{card.preset.preset_id} · v{card.preset.version}</span>
          <span className="ml-auto shrink-0 tabular-nums">as_of {card.as_of}</span>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] leading-relaxed text-muted">
          <span className="rounded-full bg-background/60 px-1.5 py-0.5 tabular-nums">总命中 {card.total}</span>
          <span className="rounded-full bg-background/60 px-1.5 py-0.5 tabular-nums">入池 {card.count}</span>
          <span className="inline-flex items-center gap-1 text-secondary"><ShieldCheck className="h-3 w-3 text-accent" />确定性筛选</span>
          <span>AI 只解释证据</span>
        </div>
      </header>

      <div className="divide-y divide-border/70">
        {card.candidates.map(candidate => (
          <article key={candidate.symbol} className="px-2.5 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-accent/15 text-[9px] font-semibold text-accent tabular-nums">
                {candidate.rank}
              </span>
              <button
                type="button"
                onClick={() => setPreview({ symbol: candidate.symbol, name: candidate.name })}
                className="min-w-0 text-left text-[11px] font-medium text-foreground hover:text-accent"
                aria-label={`查看股票 ${candidate.name} ${candidate.symbol}`}
              >
                <span>{candidate.name}</span>
                <code className="ml-1.5 text-[10px] font-normal text-muted">{candidate.symbol}</code>
              </button>
              <button
                type="button"
                onClick={() => setPreview({ symbol: candidate.symbol, name: candidate.name })}
                className="ml-auto inline-flex h-6 shrink-0 items-center gap-1 rounded-btn border border-border bg-background/50 px-1.5 text-[10px] text-secondary hover:text-foreground"
              >
                <Eye className="h-3 w-3" />查看
              </button>
            </div>
            <dl className="mt-1.5 grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-2">
              {candidate.evidence.map(evidence => (
                <div key={evidence.field} className="flex min-w-0 items-baseline gap-1 text-[10px] leading-relaxed">
                  <dt className="shrink-0 text-muted">{evidence.label}</dt>
                  <dd className="min-w-0 truncate text-secondary" title={`${evidence.display} · ${evidence.criterion}`}>
                    {evidence.display} <span className="text-muted">· {evidence.criterion}</span>
                  </dd>
                </div>
              ))}
            </dl>
          </article>
        ))}
        {!hasCandidates && (
          <div className="px-2.5 py-4 text-center text-[11px] leading-relaxed text-muted">
            当前最新可信交易日没有符合固定条件的标的；数据更新后可再次运行。
          </div>
        )}
      </div>

      <footer className="border-t border-border/80 px-2.5 py-2">
        <div className="flex flex-wrap gap-1.5">
          <button
            type="button"
            disabled={!hasCandidates || batchAdd.isPending}
            onClick={handleBatchAdd}
            className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-background/50 px-2 text-[10px] text-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {batchAdd.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <BookmarkPlus className="h-3 w-3" />}
            批量加自选
          </button>
          <button
            type="button"
            disabled={!hasCandidates}
            onClick={handleBacktest}
            className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-background/50 px-2 text-[10px] text-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            <LineChart className="h-3 w-3" />送策略回测
          </button>
        </div>
        <p className="mt-1.5 text-[10px] leading-relaxed text-muted/80">研究观察池，非投资建议。</p>
      </footer>

      <StockPreviewDialog
        symbol={preview?.symbol ?? null}
        name={preview?.name}
        onClose={() => setPreview(null)}
      />
    </section>
  )
}
