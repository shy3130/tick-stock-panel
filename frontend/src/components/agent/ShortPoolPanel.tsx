import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, BookmarkPlus, ClipboardCheck, Eye, Filter, LineChart, Loader2, ShieldCheck, X } from 'lucide-react'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { toast } from '@/components/Toast'
import { confirmTSuitabilityHypothesis } from '@/features/research/api/evidence'
import { researchKeys } from '@/features/research/queryKeys'
import { T_RESEARCH_PROTOCOL, type ShortPoolCard } from '@/lib/shortPoolCard'
import { stageScreenerBacktestHandoff } from '@/lib/screenerBacktestHandoff'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { useNavigate } from 'react-router-dom'

interface ShortPoolPanelProps {
  card: ShortPoolCard
}

const MARKET_STATE_LABEL = {
  concentrated: '抱团 / 拥挤',
  transition: '过渡',
  dispersed: '分散',
  unavailable: '不可用',
} as const

function marketResearchReasons(card: ShortPoolCard): string[] {
  if (card.candidates.length === 0) return ['当前观察池没有候选，研究动作保持关闭。']
  if (!card.market_state || !card.t_research) return ['市场状态或固定研究协议未通过完整校验，研究动作保持关闭。']
  const state = card.market_state
  const reasons = [...state.gates.reasons, ...(state.reason ? [state.reason] : []), ...state.warnings]
  return reasons.length ? reasons : ['市场状态不是分散，研究动作保持关闭。']
}


function TResearchConfirmDialog({ card, onClose }: { card: ShortPoolCard; onClose: () => void }) {
  const qc = useQueryClient()
  const create = useMutation({
    mutationFn: () => confirmTSuitabilityHypothesis({
      pool_id: card.pool_id,
      as_of: card.as_of,
      limit: card.limit,
    }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: researchKeys.hypothesesRoot })
      toast('已创建做T研究假设；未自动运行回测。', 'success')
      onClose()
    },
    onError: () => toast('创建研究假设失败', 'error'),
  })
  const state = card.market_state

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-3" role="presentation">
      <button type="button" className="absolute inset-0 bg-base/75" aria-label="取消创建做T研究假设" onClick={onClose} disabled={create.isPending} />
      <section className="relative z-10 max-h-[calc(100vh-1.5rem)] w-full max-w-xl overflow-y-auto rounded-card border border-border bg-surface p-4 shadow-xl" role="dialog" aria-modal="true" aria-labelledby="t-research-confirm-title">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 id="t-research-confirm-title" className="text-sm font-semibold text-foreground">确认创建做T研究假设</h3>
            <p className="mt-1 text-[11px] leading-relaxed text-muted">确认后只会新建一条 exploring 研究假设；不会自动运行回测、不会输出买卖点。</p>
          </div>
          <button type="button" className="rounded-btn p-1 text-muted hover:bg-elevated hover:text-foreground" aria-label="取消" onClick={onClose} disabled={create.isPending}><X className="h-4 w-4" /></button>
        </div>

        <dl className="mt-3 space-y-2 rounded-input border border-border bg-base/30 p-2.5 text-[11px] leading-relaxed">
          <div><dt className="text-muted">候选</dt><dd className="mt-0.5 text-secondary">{card.candidates.map(candidate => `${candidate.name}（${candidate.symbol}）`).join('、')}</dd></div>
          <div><dt className="text-muted">T-1 市场状态</dt><dd className="mt-0.5 text-secondary">{state ? `${MARKET_STATE_LABEL[state.state]} · ${state.signal_date ?? '不可用'}` : '不可用'}</dd></div>
          <div><dt className="text-muted">协议</dt><dd className="mt-0.5 font-mono text-secondary">{T_RESEARCH_PROTOCOL.protocol_id} · 5m · 120 sessions · min 30 events · strict_walk_forward · T-1 · 20 bps（10 / 20 / 30 敏感性）</dd></div>
          <div><dt className="text-muted">执行边界</dt><dd className="mt-0.5 text-secondary">不会自动运行。创建后仅留存研究假设，不是买卖点，非投资建议。</dd></div>
        </dl>

        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onClose} disabled={create.isPending} className="btn-secondary text-xs">取消</button>
          <button type="button" onClick={() => create.mutate()} disabled={create.isPending} className="btn-primary text-xs">
            {create.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ClipboardCheck className="h-3.5 w-3.5" />}确认创建假设
          </button>
        </div>
      </section>
    </div>
  )
}

export function ShortPoolPanel({ card }: ShortPoolPanelProps) {
  const navigate = useNavigate()
  const batchAdd = useWatchlistBatchAdd()
  const [preview, setPreview] = useState<{ symbol: string; name: string } | null>(null)
  const [confirmResearch, setConfirmResearch] = useState(false)
  const symbols = card.candidates.map(candidate => candidate.symbol)
  const hasCandidates = symbols.length > 0
  const researchReady = hasCandidates && card.market_state?.state === 'dispersed' && card.t_research?.status === 'ready_for_confirmation'
  const blockedReasons = marketResearchReasons(card)

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
          <span className="inline-flex items-center gap-1 font-medium text-secondary"><Filter className="h-3 w-3 text-accent" />短线动量质量观察</span>
          <span className="font-mono">{card.preset.preset_id} · v{card.preset.version}</span>
          <span className="ml-auto shrink-0 tabular-nums">as_of {card.as_of}</span>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] leading-relaxed text-muted">
          <span className="rounded-full bg-background/60 px-1.5 py-0.5 tabular-nums">总命中 {card.total}</span>
          <span className="rounded-full bg-background/60 px-1.5 py-0.5 tabular-nums">入池 {card.count}</span>
          <span className="inline-flex items-center gap-1 text-secondary"><ShieldCheck className="h-3 w-3 text-accent" />个股预筛轴 + 市场状态轴</span>
          <span>个股预筛为确定性筛选；AI 只解释证据</span>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 border-t border-border/60 pt-1.5 text-[10px]">
          <span className="text-muted">市场状态轴</span>
          <span className={card.market_state?.state === 'dispersed' ? 'text-success' : 'text-warning'}>{card.market_state ? `${MARKET_STATE_LABEL[card.market_state.state]} · T-1 ${card.market_state.signal_date ?? '不可用'}` : '不可用'}</span>
          <span className="text-muted">· 未复刻视频隐藏公式</span>
        </div>
      </header>

      <div className="divide-y divide-border/70">
        {card.candidates.map(candidate => (
          <article key={candidate.symbol} className="px-2.5 py-2">
            <div className="flex min-w-0 items-center gap-2">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-accent/15 text-[9px] font-semibold text-accent tabular-nums">{candidate.rank}</span>
              <button type="button" onClick={() => setPreview({ symbol: candidate.symbol, name: candidate.name })} className="min-w-0 text-left text-[11px] font-medium text-foreground hover:text-accent" aria-label={`查看股票 ${candidate.name} ${candidate.symbol}`}>
                <span>{candidate.name}</span><code className="ml-1.5 text-[10px] font-normal text-muted">{candidate.symbol}</code>
              </button>
              <button type="button" onClick={() => setPreview({ symbol: candidate.symbol, name: candidate.name })} className="ml-auto inline-flex h-6 shrink-0 items-center gap-1 rounded-btn border border-border bg-background/50 px-1.5 text-[10px] text-secondary hover:text-foreground"><Eye className="h-3 w-3" />查看</button>
            </div>
            <dl className="mt-1.5 grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-2">
              {candidate.evidence.map(evidence => <div key={evidence.field} className="flex min-w-0 items-baseline gap-1 text-[10px] leading-relaxed"><dt className="shrink-0 text-muted">{evidence.label}</dt><dd className="min-w-0 truncate text-secondary" title={`${evidence.display} · ${evidence.criterion}`}>{evidence.display} <span className="text-muted">· {evidence.criterion}</span></dd></div>)}
            </dl>
          </article>
        ))}
        {!hasCandidates && <div className="px-2.5 py-4 text-center text-[11px] leading-relaxed text-muted">当前最新可信交易日没有符合固定条件的标的；数据更新后可再次运行。</div>}
      </div>

      <footer className="border-t border-border/80 px-2.5 py-2">
        <div className="flex flex-wrap gap-1.5">
          <button type="button" disabled={!hasCandidates || batchAdd.isPending} onClick={handleBatchAdd} className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-background/50 px-2 text-[10px] text-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50">{batchAdd.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <BookmarkPlus className="h-3 w-3" />}批量加自选</button>
          <button type="button" disabled={!hasCandidates} onClick={handleBacktest} className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-background/50 px-2 text-[10px] text-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"><LineChart className="h-3 w-3" />送策略回测</button>
          {researchReady && <button type="button" onClick={() => setConfirmResearch(true)} className="inline-flex h-7 items-center gap-1 rounded-btn border border-accent/40 bg-accent/10 px-2 text-[10px] text-accent hover:bg-accent/15"><ClipboardCheck className="h-3 w-3" />创建做T研究假设</button>}
        </div>
        {!researchReady && <div className="mt-1.5 rounded-btn border border-warning/25 bg-warning/5 px-2 py-1.5 text-[10px] leading-relaxed text-warning"><p className="flex items-center gap-1 font-medium"><AlertCircle className="h-3 w-3" />做T研究动作已阻断</p><ul className="mt-0.5 list-disc pl-3.5">{blockedReasons.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}</ul></div>}
        <p className="mt-1.5 text-[10px] leading-relaxed text-muted/80">研究观察池，非投资建议；不是买卖点。</p>
      </footer>

      <StockPreviewDialog symbol={preview?.symbol ?? null} name={preview?.name} onClose={() => setPreview(null)} />
      {confirmResearch && researchReady && <TResearchConfirmDialog card={card} onClose={() => setConfirmResearch(false)} />}
    </section>
  )
}
