import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Info } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/**
 * 市场宽度(R3) 风控提醒横幅 —— 非阻断, 只读。
 *
 * 数据来源: /api/regime/breadth/latest(后端 20 日涨跌家数比 + 档位)。
 * 定位: 研究结论的「提醒」, 不是交易信号, 不触发任何调仓/配平。
 *
 * 展示策略(避免骚扰):
 *   - 低档(low): 醒目黄色横幅「市场宽度转弱, 建议降低总仓位(参考约三成)」
 *   - 中档(mid): 弱提示(灰色细条), 只说明当前中性, 不劝动作
 *   - 高档(high)/无数据/窗口不足: 不渲染
 * 文案限定「大盘/中盘宽度」—— 研究中证1000 上前后段方向不一致, 不做全市场universal 声明。
 */
export function BreadthBanner() {
  const { data } = useQuery({
    queryKey: QK.regimeBreadth(1),
    queryFn: () => api.regimeBreadthLatest(),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  })

  const latest = data?.latest
  const bucket = latest?.bucket ?? null
  // 高档 / 窗口不足(无 bucket) → 不打扰
  if (!bucket || bucket === 'high') return null

  const asOf = latest?.date ?? '—'
  const v = latest?.v_breadth
  const vText = typeof v === 'number' ? v.toFixed(3) : '—'
  const source = data?.threshold_source ?? ''
  const note = data?.note ?? '研究备注 · 非交易信号'

  if (bucket === 'low') {
    return (
      <div className="mb-1.5 flex items-start gap-2 rounded-card border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
        <div className="min-w-0 flex-1 text-secondary">
          <span className="font-medium text-foreground">市场宽度转弱</span>
          <span className="ml-1">(大盘/中盘 20 日涨跌家数比处低档: {vText}, 截至 {asOf}),</span>
          <span className="ml-1">建议降低总仓位(参考约三成)。</span>
          <span className="ml-1 text-muted">
            仅风控提醒, 非买卖信号, 不自动调仓。{source ? `阈值: ${source}。` : ''}
          </span>
          <span className="ml-1 text-muted">{note}</span>
        </div>
      </div>
    )
  }

  // mid: 弱提示, 不劝动作
  return (
    <div className="mb-1.5 flex items-start gap-2 rounded-card border border-border bg-surface/60 px-3 py-1.5 text-[11px] leading-relaxed">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
      <div className="min-w-0 flex-1 text-muted">
        <span>市场宽度中性</span>
        <span className="ml-1">(大盘/中盘 20 日涨跌家数比: {vText}, 截至 {asOf})。</span>
        <span className="ml-1">仅风控提示, 非买卖信号。{note}</span>
      </div>
    </div>
  )
}
