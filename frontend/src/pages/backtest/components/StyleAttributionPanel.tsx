import { useMutation } from '@tanstack/react-query'
import { Loader2, Shapes } from 'lucide-react'
import { api, type StrategyBacktestRequest } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { useStrategyCheckStatus } from './StrategyCheckPanel'

interface Props {
  request: StrategyBacktestRequest
}

const finiteNumber = (value: unknown): number | null => {
  if (value == null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const fmt = (value: unknown, digits = 2) => {
  const parsed = finiteNumber(value)
  return parsed == null ? '—' : parsed.toFixed(digits)
}

const FACTOR_LABELS: Array<{ key: 'smb' | 'umd' | 'lmv'; label: string; note: string }> = [
  { key: 'smb', label: 'SMB（规模）', note: '小盘 − 大盘，市值三分位多空' },
  { key: 'umd', label: 'UMD（动量）', note: '赢家 − 输家，mom_252_21 三分位多空' },
  { key: 'lmv', label: 'LMV（流动性）', note: '低换手 − 高换手，vol_60 代理的三分位多空' },
]

/** 风格归因面板 — 按钮触发; 逐日截面构造 smb/umd/lmv 三因子, 对策略日收益做 OLS 回归 */
export function StyleAttributionPanel({ request }: Props) {
  const onStatusChange = useStrategyCheckStatus('style')
  const mutation = useMutation({
    mutationFn: () => api.strategyStyleAttribution(request),
    onMutate: () => { onStatusChange?.('running') },
    onSuccess: () => { onStatusChange?.('completed') },
    onError: (error) => {
      onStatusChange?.('failed', error instanceof Error ? error.message : '风格归因失败')
    },
  })
  const attribution = mutation.data?.style_attribution
  const meta = mutation.data?.style_factor_meta
  return (
    <section className="rounded-btn border border-border bg-surface/40">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2.5">
        <div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-secondary"><Shapes className="h-3.5 w-3.5 text-accent" />风格归因（SMB / UMD / LMV）</div>
          <div className="mt-0.5 text-[10px] leading-4 text-muted">用回测股票池逐日截面构造规模/动量/流动性三因子，对策略日收益回归：α 为剥离风格暴露后的剩余收益；普通 OLS 标准误，未做 Newey-West 修正。</div>
        </div>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Shapes className="h-3.5 w-3.5" />}
          {mutation.isPending ? '回归中…' : '风格归因'}
        </button>
      </div>

      {mutation.error && (
        <div className="mx-3 mt-3 rounded-input border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] text-danger">
          {mutation.error instanceof Error ? mutation.error.message : '风格归因失败'}
        </div>
      )}

      {mutation.data && (
        <div className="space-y-3 p-3">
          {meta && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted">
              <span>因子有效日 <span className="font-mono text-secondary">{meta.valid_days}</span> / 跳过 <span className="font-mono text-secondary">{meta.skipped_days}</span></span>
              <span title="进入因子构造的日均截面宽度">截面中位数 <span className="font-mono text-secondary">{meta.median_cross_section != null ? meta.median_cross_section.toFixed(0) : '—'}</span> 只</span>
              <span>最小截面 <span className="font-mono text-secondary">{meta.min_cross_section}</span> 只</span>
              <span className="font-mono">因子口径 {meta.factor_version}</span>
            </div>
          )}
          {attribution == null ? (
            <div className="rounded-input border border-border px-3 py-3 text-center text-[11px] leading-5 text-muted">
              有效日不足：可构造因子的交易日不足 120 天（或回归样本 &lt; 120），无法给出可信的风格暴露估计；可拉长区间或扩大股票池后重试。
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                <div className="rounded-input border border-border bg-base/30 px-3 py-2" title="剥离三因子暴露后的每期 α（日频小数）">
                  <div className="text-[10px] text-muted">α（每期）</div>
                  <div className={`mt-1 font-mono text-sm font-semibold num ${priceColorClass(attribution.alpha_per_period)}`}>{fmt(attribution.alpha_per_period, 5)}</div>
                </div>
                <div className="rounded-input border border-border bg-base/30 px-3 py-2" title="α × 每年期数（复利近似前线性年化）">
                  <div className="text-[10px] text-muted">α（年化）</div>
                  <div className={`mt-1 font-mono text-sm font-semibold num ${priceColorClass(attribution.alpha_annualized)}`}>{fmtPct(attribution.alpha_annualized)}</div>
                </div>
                <div className="rounded-input border border-border bg-base/30 px-3 py-2" title="回归可解释方差占比">
                  <div className="text-[10px] text-muted">R²</div>
                  <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{fmt(attribution.r_squared)}</div>
                </div>
                <div className="rounded-input border border-border bg-base/30 px-3 py-2" title="进入回归的交易日样本数">
                  <div className="text-[10px] text-muted">回归样本</div>
                  <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{attribution.n_obs} 日</div>
                </div>
              </div>
              <div className="data-table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>因子</th>
                      <th>构造口径</th>
                      <th className="text-right">β</th>
                      <th className="text-right">t 值</th>
                      <th className="text-right">显著性</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="font-medium text-foreground">α（截距）</td>
                      <td className="text-[10px] text-muted">剥离风格后的剩余收益</td>
                      <td className={`text-right font-mono num ${priceColorClass(attribution.alpha_per_period)}`}>{fmt(attribution.alpha_per_period, 5)}</td>
                      <td className="text-right font-mono num">{fmt(attribution.t_stats.alpha)}</td>
                      <td className="text-right">{Math.abs(finiteNumber(attribution.t_stats.alpha) ?? 0) >= 2 ? <span className="text-accent">|t| ≥ 2</span> : <span className="text-muted">不显著</span>}</td>
                    </tr>
                    {FACTOR_LABELS.map(factor => {
                      const tStat = finiteNumber(attribution.t_stats[factor.key])
                      return (
                        <tr key={factor.key}>
                          <td className="font-medium text-foreground">{factor.label}</td>
                          <td className="text-[10px] text-muted">{factor.note}</td>
                          <td className="text-right font-mono num">{fmt(attribution.betas[factor.key])}</td>
                          <td className="text-right font-mono num">{fmt(tStat)}</td>
                          <td className="text-right">{tStat != null && Math.abs(tStat) >= 2 ? <span className="text-accent">|t| ≥ 2</span> : <span className="text-muted">不显著</span>}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <div className="text-[10px] leading-4 text-muted">
                t 值阈值按 |t| ≥ 2 粗略判定（普通 OLS 标准误，日频残差自相关/异方差时偏乐观）；β 显著 ≠ 因果，仅说明策略收益与该风格因子共动。因子口径 {attribution.factor_version}。
              </div>
              <div className="text-[10px] text-muted font-mono">run_id: {mutation.data.run_id}</div>
            </>
          )}
        </div>
      )}
    </section>
  )
}
