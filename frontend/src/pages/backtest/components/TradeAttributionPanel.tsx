import type { TradeIndustryAttribution } from '@/lib/api'

interface Props {
  attribution?: TradeIndustryAttribution | null
}

const REASON_LABELS: Record<string, string> = {
  no_completed_trades: '没有已完成交易，无法计算归因。',
  no_industry_mapping: '没有可映射的行业分类，无法计算归因。',
  insufficient_classified_trades: '可归类的已完成交易不足两笔，无法计算归因。',
  insufficient_industries: '有效交易不足两个行业，无法进行行业间归因。',
  candidate_execution_not_portfolio_attribution: '独立候选执行不是资金受约束的账户组合，不能生成行业归因。',
}

function finite(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function pct(value: number | null | undefined, digits = 2): string {
  const number = finite(value)
  return number == null ? '—' : `${(number * 100).toFixed(digits)}%`
}

function signedPct(value: number | null | undefined, digits = 2): string {
  const number = finite(value)
  if (number == null) return '—'
  return `${number > 0 ? '+' : ''}${(number * 100).toFixed(digits)}%`
}

function valueClass(value: number | null | undefined): string {
  const number = finite(value)
  if (number == null || number === 0) return 'text-secondary'
  return number > 0 ? 'text-bull' : 'text-bear'
}

function statusLabel(status: string): string {
  if (status === 'ok') return '可用'
  if (status === 'insufficient_data') return '数据不足'
  return '不可用'
}

/**
 * 基于已完成交易的行业 Brinson-Fachler 报告。
 * 只消费后端已冻结在 Run 中的归因输出；不在前端重算、也不把它表述为官方指数归因。
 */
export function TradeAttributionPanel({ attribution }: Props) {
  if (!attribution) return null

  const brinson = attribution.brinson
  const isReady = attribution.status === 'ok' && brinson?.status === 'ok'
  const reason = attribution.reason ? REASON_LABELS[attribution.reason] ?? attribution.reason : null

  return (
    <section className="overflow-hidden rounded-btn border border-border" aria-labelledby="trade-attribution-heading">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-3 py-2">
        <div>
          <h3 id="trade-attribution-heading" className="text-xs font-medium text-foreground">交易窗口行业归因</h3>
          <p className="mt-0.5 max-w-3xl text-[10px] leading-4 text-muted">{attribution.scope}</p>
        </div>
        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${isReady
          ? 'border-bull/30 bg-bull/10 text-bull'
          : 'border-warning/30 bg-warning/10 text-warning'}`}
        >
          {statusLabel(attribution.status)}
        </span>
      </div>

      <div className="space-y-3 p-3">
        <p className="text-[10px] leading-4 text-muted">{attribution.classification_note}</p>

        {isReady && brinson ? (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="价值加权交易样本" value={pct(brinson.portfolio_return)} />
              <Metric label="等权交易样本" value={pct(brinson.benchmark_return)} />
              <Metric label="相对差异" value={signedPct(brinson.excess_return)} tone={brinson.excess_return} />
              <Metric label="资金覆盖" value={pct(attribution.capital_coverage, 1)} />
            </div>

            <div className="grid grid-cols-3 gap-2">
              <Metric label="配置效应" value={signedPct(brinson.allocation)} tone={brinson.allocation} />
              <Metric label="选股效应" value={signedPct(brinson.selection)} tone={brinson.selection} />
              <Metric label="交互效应" value={signedPct(brinson.interaction)} tone={brinson.interaction} />
            </div>

            <div className="data-table-scroll rounded-btn border border-border">
              <table className="data-table min-w-[760px]">
                <thead className="bg-elevated">
                  <tr className="text-left text-secondary">
                    <th className="px-3 py-2 font-medium">行业</th>
                    <th className="px-3 py-2 text-right font-medium">组合权重</th>
                    <th className="px-3 py-2 text-right font-medium">等权基准</th>
                    <th className="px-3 py-2 text-right font-medium">组合收益</th>
                    <th className="px-3 py-2 text-right font-medium">等权收益</th>
                    <th className="px-3 py-2 text-right font-medium">配置</th>
                    <th className="px-3 py-2 text-right font-medium">选股</th>
                    <th className="px-3 py-2 text-right font-medium">交互</th>
                  </tr>
                </thead>
                <tbody>
                  {brinson.groups.map(group => (
                    <tr key={group.group} className="border-t border-border hover:bg-elevated/50">
                      <td className="px-3 py-2 font-medium text-foreground">{group.group}</td>
                      <td className="px-3 py-2 text-right font-mono num text-secondary">{pct(group.portfolio_weight, 1)}</td>
                      <td className="px-3 py-2 text-right font-mono num text-secondary">{pct(group.benchmark_weight, 1)}</td>
                      <td className={`px-3 py-2 text-right font-mono num ${valueClass(group.portfolio_return)}`}>{signedPct(group.portfolio_return)}</td>
                      <td className={`px-3 py-2 text-right font-mono num ${valueClass(group.benchmark_return)}`}>{signedPct(group.benchmark_return)}</td>
                      <td className={`px-3 py-2 text-right font-mono num ${valueClass(group.allocation)}`}>{signedPct(group.allocation)}</td>
                      <td className={`px-3 py-2 text-right font-mono num ${valueClass(group.selection)}`}>{signedPct(group.selection)}</td>
                      <td className={`px-3 py-2 text-right font-mono num ${valueClass(group.interaction)}`}>{signedPct(group.interaction)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <div className="rounded-btn border border-warning/25 bg-warning/5 px-3 py-2 text-[11px] leading-5 text-secondary" role="status">
            {reason ?? '归因计算所需的数据不可用。'}
            <span className="ml-1 text-muted">已归类 {attribution.classified_trades} / {attribution.input_trades} 笔，资金覆盖 {pct(attribution.capital_coverage, 1)}。</span>
          </div>
        )}

        <div className="rounded-btn border border-border bg-base/40 px-3 py-2 text-[10px] leading-4 text-muted">
          <span className="font-medium text-secondary">Fama-French：</span>
          {attribution.fama_french.detail}
        </div>

        {attribution.warnings.length > 0 && (
          <ul className="space-y-1 text-[10px] leading-4 text-warning">
            {attribution.warnings.map((warning, index) => <li key={`${warning}-${index}`}>• {warning}</li>)}
          </ul>
        )}
      </div>
    </section>
  )
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: number | null }) {
  return (
    <div className="rounded-btn border border-border bg-base/40 px-2.5 py-2">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-sm font-medium num ${tone === undefined ? 'text-foreground' : valueClass(tone)}`}>{value}</div>
    </div>
  )
}
