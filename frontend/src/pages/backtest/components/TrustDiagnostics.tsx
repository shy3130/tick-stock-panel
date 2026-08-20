import { useMemo } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Activity, CheckCircle2, Crosshair, Loader2, ShieldQuestion } from 'lucide-react'
import {
  api,
  type FillReachabilityResponse,
  type ListingAgeGateStats,
  type StrategyBacktestRequest,
  type StrategyBacktestResult,
  type StrategyCapacityStats,
} from '@/lib/api'
import { fmtBigNum, fmtPct } from '@/lib/format'

interface Props {
  result: StrategyBacktestResult
  /** 当前回测请求配置 (缺省时回退读 result.config), 用于“请求了但未启用”的提示 */
  request?: StrategyBacktestRequest | null
}

const finiteNumber = (value: unknown): number | null => {
  if (value == null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

const asRecord = (value: unknown): Record<string, any> | null =>
  value != null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, any>) : null

const fmt = (value: unknown, digits = 2) => {
  const parsed = finiteNumber(value)
  return parsed == null ? '—' : parsed.toFixed(digits)
}

/** 容量诊断块: stats.capacity 存在且 enabled 时渲染; 请求了量能约束但未启用时给“未启用”提示 */
function CapacitySection({ result, request }: Props) {
  const capacity = asRecord(result.stats?.capacity) as StrategyCapacityStats | null
  const requestedViaConfig = finiteNumber(request?.max_participation_pct) != null
    || finiteNumber((result.config ?? {}).max_participation_pct) != null
  if (capacity?.enabled) {
    const unconstrained = capacity.unconstrained === true
    return (
      <section className="overflow-hidden rounded-btn border border-border">
        <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
          <div>
            <div className="text-xs font-medium text-foreground">策略容量（量能约束）</div>
            <div className="mt-0.5 text-[10px] leading-4 text-muted">
              单笔上限名义金额 = min(当日量, 均量窗口日均量) × 参与率 × 成交价；利用率为实际成交额 ÷ 上限额
            </div>
          </div>
          {unconstrained && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-bull/30 bg-bull/10 px-2 py-0.5 text-[10px] font-medium text-bull">
              <CheckCircle2 className="h-3 w-3" />未触及量能约束
            </span>
          )}
        </div>
        <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
          <div className="bg-surface px-3 py-2.5" title="受量能上限截断 (实际买入 < 目标) 的建仓笔数">
            <div className="text-[10px] text-muted">被截断建仓</div>
            <div className={`mt-1 font-mono text-sm font-semibold num ${capacity.capped_entry_count > 0 ? 'text-warning' : 'text-foreground'}`}>{capacity.capped_entry_count} 笔</div>
          </div>
          <div className="bg-surface px-3 py-2.5" title="量能利用率 = 实际成交名义金额 / 单笔量能上限">
            <div className="text-[10px] text-muted">利用率 p50 / p90</div>
            <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{fmtPct(capacity.utilization_p50, 1)} / {fmtPct(capacity.utilization_p90, 1)}</div>
          </div>
          <div className="bg-surface px-3 py-2.5" title="单笔量能上限名义金额 (元) 的分位；p10 代表最紧张的一档">
            <div className="text-[10px] text-muted">单笔上限额 p50 / p10</div>
            <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{fmtBigNum(capacity.cap_value_p50)} / {fmtBigNum(capacity.cap_value_p10)}</div>
          </div>
          <div className="bg-surface px-3 py-2.5" title="线性外推近似：假设成交价与滚动量能不随资金规模变化，且未计多笔同日抢同一上限的挤占">
            <div className="text-[10px] text-muted">容量倍数估计</div>
            <div className="mt-1 font-mono text-sm font-semibold text-foreground num">
              {capacity.est_capacity_multiple != null ? `≈ ${capacity.est_capacity_multiple.toFixed(2)}x` : '—'}
            </div>
          </div>
        </div>
        <div className="border-t border-border px-3 py-2 text-[10px] leading-4 text-muted">
          {capacity.est_capacity_multiple != null
            ? `当前资金规模约可放大 ${capacity.est_capacity_multiple.toFixed(2)} 倍（p90 笔仍不触碰量能上限）；该估计为线性外推口径，非精确容量解。`
            : '样本不足，未输出容量倍数估计。'}
          {capacity.unconstrained === false && ' 存在被量能约束压缩的建仓，解读收益时需同时看资金约束与量能约束的叠加影响。'}
        </div>
      </section>
    )
  }
  if (requestedViaConfig) {
    return (
      <section className="rounded-btn border border-border px-3 py-2.5">
        <div className="flex items-start gap-2">
          <ShieldQuestion className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
          <div className="text-[11px] leading-5 text-muted">
            <span className="font-medium text-secondary">容量诊断未启用。</span>
            请求里带了最大参与率，但本次结果未能量化量能约束（旧版本结果或量能约束未生效）；重新运行后可见。
          </div>
        </div>
      </section>
    )
  }
  return null
}

/** PSR 指标卡: Sharpe 经偏度/峰度与样本量校正后 &gt; 0 的概率 */
function PsrSection({ result }: { result: StrategyBacktestResult }) {
  const psr = finiteNumber(result.stats?.psr)
  if (psr == null) return null
  return (
    <section className="rounded-btn border border-border bg-surface px-3 py-2.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[10px] text-muted">PSR 概率</span>
        <span className={`font-mono text-sm font-semibold num ${psr >= 0.95 ? 'text-bull' : psr < 0.5 ? 'text-bear' : 'text-foreground'}`}>
          {(psr * 100).toFixed(1)}%
        </span>
      </div>
      <div className="mt-1 text-[9px] leading-3.5 text-muted">Sharpe 经偏度/峰度与样本量校正后 &gt; 0 的概率；越高说明正 Sharpe 越不像是小样本运气。</div>
    </section>
  )
}

/** 上市天数门控统计: 行数统计 + 未知上市日计数; requested=true 时黄色告警 reason */
function ListingAgeGateSection({ result }: { result: StrategyBacktestResult }) {
  const gate = asRecord(result.stats?.listing_age_gate) as ListingAgeGateStats | null
  if (gate == null) return null
  if (!gate.enabled) {
    if (gate.requested) {
      return (
        <section className="rounded-btn border border-warning/30 bg-warning/5 px-3 py-2.5">
          <div className="flex items-start gap-2">
            <ShieldQuestion className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            <div className="text-[11px] leading-5 text-warning">
              <span className="font-medium">上市天数门控未生效：</span>{gate.reason ?? '上市日期数据不可用'}。本次回测未过滤次新股，幸存者口径解读需谨慎。
            </div>
          </div>
        </section>
      )
    }
    return null
  }
  return (
    <section className="overflow-hidden rounded-btn border border-border">
      <div className="border-b border-border px-3 py-2">
        <div className="text-xs font-medium text-foreground">上市天数门控</div>
        <div className="mt-0.5 text-[10px] text-muted">上市不足 {gate.min_listed_days} 天的标的整段不入面板（删行口径，非入场过滤）</div>
      </div>
      <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
        <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">过滤前面板行</div><div className="mt-1 font-mono text-sm text-foreground num">{gate.rows_before?.toLocaleString('zh-CN') ?? '—'}</div></div>
        <div className="bg-surface px-3 py-2.5"><div className="text-[10px] text-muted">被过滤行</div><div className={`mt-1 font-mono text-sm num ${gate.rows_dropped ? 'text-warning' : 'text-foreground'}`}>{gate.rows_dropped?.toLocaleString('zh-CN') ?? '—'}</div></div>
        <div className="bg-surface px-3 py-2.5" title="整段被滤掉（所有行都不满足门控）的 symbol 数"><div className="text-[10px] text-muted">整段滤掉标的</div><div className="mt-1 font-mono text-sm text-foreground num">{gate.symbols_dropped ?? '—'}</div></div>
        <div className="bg-surface px-3 py-2.5" title="上市日期缺失无法判定、被 fail-open 保留的行数"><div className="text-[10px] text-muted">上市日未知行</div><div className={`mt-1 font-mono text-sm num ${gate.unknown_listing_date ? 'text-warning' : 'text-foreground'}`}>{gate.unknown_listing_date?.toLocaleString('zh-CN') ?? '—'}</div></div>
      </div>
    </section>
  )
}

const SIDE_LABEL: Record<string, string> = { entry: '买入', exit: '卖出' }

/** 成交可达性诊断: 按钮触发, 对最近一次 result.run_id 抽样检查成交价价格带内分钟成交额 */
function FillReachabilitySection({ runId }: { runId: string }) {
  const mutation = useMutation({
    mutationFn: () => api.backtestFillReachability(runId),
  })
  const fr: FillReachabilityResponse['fill_reachability'] | undefined = mutation.data?.fill_reachability
  return (
    <section className="overflow-hidden rounded-btn border border-border">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2">
        <div>
          <div className="text-xs font-medium text-foreground">成交可达性诊断</div>
          <div className="mt-0.5 text-[10px] leading-4 text-muted">抽样检查每笔成交在成交价 ±{fr ? `${(fr.price_band_pct * 100).toFixed(1)}%` : '价格带'} 内的当日分钟成交额能否覆盖交易名义金额</div>
        </div>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Crosshair className="h-3.5 w-3.5" />}
          {mutation.isPending ? '诊断中…' : '诊断成交可达性'}
        </button>
      </div>
      {mutation.error && (
        <div className="border-b border-border bg-danger/5 px-3 py-2 text-[11px] text-danger">
          {mutation.error instanceof Error ? mutation.error.message : '成交可达性诊断失败'}
        </div>
      )}
      {fr && (
        <div className="space-y-2 p-3">
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            <div className="rounded-input border border-border bg-base/30 px-3 py-2">
              <div className="text-[10px] text-muted">价格带内可达占比</div>
              <div className={`mt-1 font-mono text-sm font-semibold num ${fr.reachable_pct >= 0.95 ? 'text-bull' : fr.reachable_pct < 0.8 ? 'text-bear' : 'text-warning'}`}>{fmtPct(fr.reachable_pct, 1)}</div>
            </div>
            <div className="rounded-input border border-border bg-base/30 px-3 py-2" title="headroom = 价格带内分钟成交额 ÷ 交易名义金额；p10 为最紧张的一档">
              <div className="text-[10px] text-muted">headroom p50 / p10</div>
              <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{fmt(fr.headroom_p50, 2)}x / {fmt(fr.headroom_p10, 2)}x</div>
            </div>
            <div className="rounded-input border border-border bg-base/30 px-3 py-2">
              <div className="text-[10px] text-muted">抽样笔数</div>
              <div className="mt-1 font-mono text-sm font-semibold text-foreground num">{fr.n_sampled} / {fr.n_trades}（seed={fr.sample_seed}）</div>
            </div>
            <div className="rounded-input border border-border bg-base/30 px-3 py-2" title="分钟数据缺失、无法判定的侧">
              <div className="text-[10px] text-muted">无数据侧</div>
              <div className={`mt-1 font-mono text-sm font-semibold num ${fr.no_data_pct > 0 ? 'text-warning' : 'text-foreground'}`}>{fr.n_no_data} 侧（{fmtPct(fr.no_data_pct, 1)}）</div>
            </div>
          </div>
          {fr.worst.length > 0 && (
            <div className="data-table-scroll rounded-input border border-border">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>标的</th><th>日期</th><th>方向</th>
                    <th className="text-right">headroom</th>
                    <th className="text-right">价格带内成交额</th>
                    <th className="text-right">交易名义金额</th>
                  </tr>
                </thead>
                <tbody>
                  {fr.worst.slice(0, 5).map(row => (
                    <tr key={`${row.symbol}-${row.date}-${row.side}`}>
                      <td className="font-mono">{row.symbol}</td>
                      <td className="font-mono text-[11px]">{String(row.date).slice(0, 10)}</td>
                      <td>{SIDE_LABEL[row.side] ?? row.side}</td>
                      <td className={`text-right font-mono num ${row.headroom != null && row.headroom < 1 ? 'text-bear' : 'text-foreground'}`}>{row.headroom != null ? `${row.headroom.toFixed(2)}x` : '—'}</td>
                      <td className="text-right font-mono num">{fmtBigNum(row.band_notional)}</td>
                      <td className="text-right font-mono num">{fmtBigNum(row.trade_notional)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {fr.note && <div className="text-[10px] leading-4 text-muted">{fr.note}</div>}
        </div>
      )}
    </section>
  )
}

/** 可信度诊断聚合面板 — 全部子区块条件渲染: 数据缺失/null 不渲染、不报错 */
export function TrustDiagnostics({ result, request }: Props) {
  const blocks = useMemo(() => ({
    hasPsr: finiteNumber(result.stats?.psr) != null,
    hasGate: asRecord(result.stats?.listing_age_gate) != null,
  }), [result])
  return (
    <div className="space-y-3">
      <CapacitySection result={result} request={request} />
      {blocks.hasPsr && (
        <div className="grid gap-3 lg:grid-cols-2">
          <PsrSection result={result} />
          <div className="rounded-btn border border-border bg-surface px-3 py-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[10px] text-muted">口径提示</span>
              <Activity className="h-3.5 w-3.5 text-muted" />
            </div>
            <div className="mt-1 text-[9px] leading-3.5 text-muted">PSR 只校正收益分布形态与样本量，不校正数据窥探/过拟合；参数寻优后的结果需再打折解读。</div>
          </div>
        </div>
      )}
      <ListingAgeGateSection result={result} />
      {result.run_id && <FillReachabilitySection runId={result.run_id} />}
    </div>
  )
}
