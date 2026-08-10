import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { HelpCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { toast } from '@/components/Toast'

/** 单方向(涨停/跌停)的修正明细块 */
function SealedDirBlock({ title, color, counts, rawTotal }: {
  title: string
  color: 'bull' | 'bear'
  counts?: { real: number; fake: number; pending: number }
  rawTotal?: number
}) {
  const real = counts?.real ?? 0
  const fake = counts?.fake ?? 0
  // pending 从原始总数推算(后端 pending 含另一方向票, 不可用)
  const pending = Math.max(0, (rawTotal ?? 0) - real - fake)
  const original = rawTotal ?? (real + fake + pending)
  const fixed = real + pending
  return (
    <div className="mb-2 last:mb-0">
      <div className={`flex items-center justify-between px-1 py-0.5 rounded bg-${color}/5 mb-1`}>
        <span className={`text-[10px] font-medium text-${color}`}>{title}</span>
        <span className="tabular-nums text-[10px]">
          <span className="text-muted line-through">{original}</span>
          <span className="text-muted/50 mx-1">→</span>
          <span className={`font-bold text-${color}`}>{fixed}</span>
        </span>
      </div>
      <div className="flex gap-3 px-1 text-[10px]">
        <span className={`flex items-center gap-0.5 text-${color}`}><span className={`h-1 w-1 rounded-full bg-${color}`} />真封 {real}</span>
        <span className="flex items-center gap-0.5 text-yellow-500"><span className="h-1 w-1 rounded-full bg-yellow-500" />假 {fake}</span>
        {pending > 0 && (
          <span className="flex items-center gap-0.5 text-muted"><span className="h-1 w-1 rounded-full bg-muted" />待 {pending}</span>
        )}
      </div>
    </div>
  )
}

/** 修正/降级/外部降级 标识 + 问号弹窗(连板梯队/看板共用) */
export function SealedBadge({
  degraded,
  hasDepth,
  isHistorical,
  sealedReady,
  sealedCountsUp,
  sealedCountsDown,
  rawUp,
  rawDown,
  invalidateKeys = ['limit-ladder'],
  sealedDegraded = false,
  sealedSource,
}: {
  degraded: boolean
  hasDepth: boolean
  isHistorical: boolean
  sealedReady: boolean | undefined
  sealedCountsUp?: { real: number; fake: number; pending: number }
  sealedCountsDown?: { real: number; fake: number; pending: number }
  rawUp?: number
  rawDown?: number
  /** 修正后要刷新的 queryKey 前缀(默认连板梯队) */
  invalidateKeys?: string[]
  /**
   * 外部 depth 展示降级(腾讯公共行情等)。仅连板页当前展示用;
   * 为 true 时徽标显示「外部降级」, 弹层说明只读边界, 不出现「立即修正」。
   */
  sealedDegraded?: boolean
  /** 外部展示来源, 如 tencent_quote */
  sealedSource?: string | null
}) {
  const [showHint, setShowHint] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const runFix = useMutation({
    mutationFn: () => api.runLimitLadderFix(),
    onSuccess: (data) => {
      toast(data.msg, data.ok ? 'success' : 'error')
      if (data.ok) invalidateKeys.forEach(k => qc.invalidateQueries({ queryKey: [k] }))
    },
    onError: () => toast('修正请求失败', 'error'),
  })

  // 外部降级优先于本地「降级/修正」文案; 不诱导写入/立即修正
  const isExternal = sealedDegraded === true

  // 组装原因文案(仅本地降级时用)
  const reasons: string[] = []
  if (!hasDepth) reasons.push('当前数据源无五档盘口能力,涨停判定基于收盘价,可能含假涨停')
  if (isHistorical) reasons.push('历史日期的盘口快照不可获取,无法判定真假板')
  if (hasDepth && !isHistorical && !sealedReady) reasons.push('盘中 sealed 数据尚未就绪,收盘后自动恢复')

  const sourceLabel =
    sealedSource === 'tencent_quote' || sealedSource === 'fallback_external'
      ? '腾讯公共行情'
      : sealedSource
        ? `外部行情(${sealedSource})`
        : '腾讯公共行情'

  const label = isExternal ? '外部降级' : degraded ? '降级' : '修正'

  // 外部降级 / 普通降级 / 本地修正: 均不在外部路径展示「立即修正」
  const canRunFix = !isExternal && hasDepth && !isHistorical

  return (
    <div className="relative inline-flex items-center">
      <button
        onClick={() => setShowHint(v => !v)}
        className={`group inline-flex items-center gap-1 h-5 px-2 rounded-full cursor-help transition-all ${
          isExternal
            ? 'bg-orange-500/10 border border-orange-500/35 hover:bg-orange-500/20 hover:border-orange-500/55'
            : 'bg-yellow-500/10 border border-yellow-500/30 hover:bg-yellow-500/20 hover:border-yellow-500/50'
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${isExternal ? 'bg-orange-500' : 'bg-yellow-500'}`} />
        <span className={`text-[10px] font-medium leading-none ${
          isExternal
            ? 'text-orange-600 dark:text-orange-400'
            : 'text-yellow-600 dark:text-yellow-500'
        }`}>{label}</span>
        <HelpCircle className={`h-3 w-3 transition-colors ${
          isExternal
            ? 'text-orange-500/70 group-hover:text-orange-500'
            : 'text-yellow-500/70 group-hover:text-yellow-500'
        }`} />
      </button>
      <AnimatePresence>
        {showHint && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setShowHint(false)} />
            <motion.div
              initial={{ opacity: 0, y: -4, scale: 0.95 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.95 }}
              className="absolute top-full left-0 mt-1 z-50 w-72 bg-surface border border-border rounded-md shadow-xl p-3 text-[11px] text-secondary leading-relaxed"
              onClick={e => e.stopPropagation()}
            >
              {isExternal ? (
                <>
                  <div className="font-medium text-foreground mb-1.5">外部盘口降级展示</div>
                  <div className="flex gap-1 mb-1">
                    <span className="text-orange-500 shrink-0">·</span>
                    <span>盘口来自{sourceLabel}, 仅补充当前连板页股票行的封单展示。</span>
                  </div>
                  <div className="flex gap-1 mb-1">
                    <span className="text-orange-500 shrink-0">·</span>
                    <span>只读边界: 不写入本地 sealed / depth5, 不参与历史 sealed 定版。</span>
                  </div>
                  <div className="flex gap-1 mb-1">
                    <span className="text-orange-500 shrink-0">·</span>
                    <span>不修正涨跌停 counts / 状态归类, 不参与选股、回测与监控口径。</span>
                  </div>
                  <div className="mt-1.5 pt-1.5 border-t border-border text-muted">
                    外部数据仅供当前页面展示对照, 权威真假板仍以本地 provider sealed 为准。
                  </div>
                </>
              ) : degraded ? (
                <>
                  <div className="font-medium text-foreground mb-1.5">真假涨停判定降级</div>
                  {reasons.map((r, i) => (
                    <div key={i} className="flex gap-1 mb-1">
                      <span className="text-yellow-500 shrink-0">·</span>
                      <span>{r}</span>
                    </div>
                  ))}
                  <div className="mt-1.5 pt-1.5 border-t border-border text-muted">
                    真假板判定依赖五档盘口实时快照(卖一/买一量)。当天数据在收盘后自动恢复。
                  </div>
                </>
              ) : (
                <>
                  <div className="font-medium text-foreground mb-1.5">五档盘口修正结果</div>
                  <SealedDirBlock title="涨停" color="bull" counts={sealedCountsUp} rawTotal={rawUp} />
                  <SealedDirBlock title="跌停" color="bear" counts={sealedCountsDown} rawTotal={rawDown} />
                  <div className="mt-1.5 pt-1.5 border-t border-border text-muted">
                    真封板显示封单量,假涨停/假跌停已归入炸板/翘板视图。{sealedReady && '数据为盘中快照,收盘后自动定版。'}
                  </div>
                </>
              )}
              <div className="mt-2 flex gap-1.5">
                {canRunFix && (
                  <button
                    onClick={() => { runFix.mutate(); setShowHint(false) }}
                    disabled={runFix.isPending}
                    className="flex-1 px-2 py-1.5 rounded text-[11px] bg-accent/15 text-accent hover:bg-accent/25 transition-colors text-center disabled:opacity-50"
                  >
                    {runFix.isPending ? '修正中…' : '立即修正'}
                  </button>
                )}
                <button
                  onClick={() => { setShowHint(false); navigate('/settings?tab=monitoring&highlight=depth-fix') }}
                  className={`${canRunFix ? '' : 'w-full'} px-2 py-1.5 rounded text-[11px] bg-elevated text-secondary hover:text-foreground transition-colors text-center`}
                >
                  去设置 →
                </button>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}
