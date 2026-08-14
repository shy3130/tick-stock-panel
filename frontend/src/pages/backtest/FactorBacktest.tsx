import { useState, useMemo } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Play, BarChart3, Clock } from 'lucide-react'
import { api, type FactorColumn, type FactorBacktestResult, type GroupStat } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { InstrumentSearchAdder } from '@/components/instruments/InstrumentSearchInput'
import { FactorICChart } from './charts/FactorICChart'
import { FactorGroupNavChart } from './charts/FactorGroupNavChart'

const formatDate = (date: Date) => date.toISOString().slice(0, 10)
const monthsAgo = (months: number) => {
  const date = new Date()
  date.setMonth(date.getMonth() - months)
  return formatDate(date)
}
const TODAY = formatDate(new Date())
const THREE_MONTHS_AGO = monthsAgo(3)

const INPUT_CLS = 'control w-full text-xs'
const appendUniqueSymbol = (symbolsText: string, symbol: string) => {
  const key = symbol.trim().toUpperCase()
  const symbols = symbolsText.split(',').map(value => value.trim()).filter(Boolean)
  return symbols.some(s => s.toUpperCase() === key) ? symbolsText : symbolsText ? `${symbolsText},${symbol}` : symbol
}


function StatCard({ label, value, highlight }: {
  label: string
  value: string | null | undefined
  highlight?: 'bull' | 'bear' | 'neutral'
}) {
  const colorCls = highlight === 'bull'
    ? 'text-bull' : highlight === 'bear' ? 'text-bear' : ''
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`metric-value mt-1 !text-base ${colorCls}`}>
        {value ?? '—'}
      </div>
    </div>
  )
}

function LoadingPanel({ symbolsText }: { symbolsText: string }) {
  return (
    <div className="space-y-3">
      <div className="panel">
        <div className="panel-body flex items-center justify-between gap-3">
          <div>
            <div className="section-title">正在计算因子分析</div>
            <div className="mt-1 text-xs text-muted">{symbolsText} · 完成后会一次性刷新 IC、分层收益和净值曲线。</div>
          </div>
          <div className="h-7 w-7 rounded-full border-2 border-border border-t-accent animate-spin" />
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {['读取因子', '计算 IC', '分层回测', '汇总指标'].map(item => (
          <div key={item} className="rounded-btn border border-border bg-elevated px-3 py-2">
            <div className="h-2 w-10 rounded bg-border animate-pulse" />
            <div className="mt-2 text-xs text-secondary">{item}</div>
          </div>
        ))}
      </div>

      <div className="panel">
        <div className="panel-header">
          <span className="section-title">分层净值预览</span>
          <span className="text-[11px] text-muted">等待后端返回完整结果</span>
        </div>
        <div className="panel-body">
          <div className="h-[220px] rounded-btn border border-border bg-elevated/60 p-4">
            <div className="flex h-full items-end gap-2 opacity-60">
              {[46, 38, 54, 50, 64, 58, 74, 68, 84, 78, 90, 86].map((h, i) => (
                <div key={i} className="flex-1 rounded-t bg-border animate-pulse" style={{ height: `${h}%` }} />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export function FactorBacktest() {
  const [factorName, setFactorName] = useState('momentum_20d')
  const [symbols, setSymbols] = useState('')
  const [start, setStart] = useState(THREE_MONTHS_AGO)
  const [end, setEnd] = useState(TODAY)
  const [nGroups, setNGroups] = useState(5)
  const [weight, setWeight] = useState<'equal' | 'factor_weight'>('equal')
  const [fees, setFees] = useState('2')
  const [result, setResult] = useState<FactorBacktestResult | null>(null)

  const columns = useQuery({
    queryKey: ['backtest-factor-columns'],
    queryFn: api.factorColumns,
  })

  // 按 group 分类的因子
  const factorGroups = useMemo(() => {
    const cols = columns.data?.columns ?? []
    const groups: Record<string, FactorColumn[]> = {}
    for (const c of cols) {
      ;(groups[c.group] ??= []).push(c)
    }
    return groups
  }, [columns.data])

  // 当前因子描述
  const factorDesc = useMemo(() => {
    return columns.data?.columns.find(c => c.id === factorName)?.desc ?? ''
  }, [columns.data, factorName])

  const run = useMutation({
    mutationFn: () =>
      api.factorRun({
        factor_name: factorName,
        symbols: symbols ? symbols.split(',').map(s => s.trim()).filter(Boolean) : null,
        start: start || null,
        end: end || undefined,
        n_groups: nGroups,
        rebalance: 'daily',
        weight,
        fees_pct: Number(fees) / 10000,
      }),
    onSuccess: (data) => {
      if (data.error) {
        setResult(data)
      } else {
        setResult(data)
      }
    },
  })

  const applyRange = (months: number) => {
    setStart(monthsAgo(months))
    setEnd(formatDate(new Date()))
  }

  const applyAllRange = () => {
    setStart('')
    setEnd(formatDate(new Date()))
  }

  const rangeKey = end === TODAY && start === THREE_MONTHS_AGO
    ? '3m'
    : end === TODAY && start === monthsAgo(6)
      ? '6m'
      : end === TODAY && start === monthsAgo(12)
        ? '1y'
        : end === TODAY && start === ''
          ? 'all'
          : 'custom'
  const rangeTitle = rangeKey === '3m'
    ? '近 3 个月'
    : rangeKey === '6m'
      ? '近 6 个月'
      : rangeKey === '1y'
        ? '近 1 年'
        : rangeKey === 'all'
          ? '全部历史'
          : '自定义区间'
  const rangeButtonCls = (key: string) => `rounded-btn px-2 py-1 text-[11px] font-medium transition-colors ${rangeKey === key
    ? 'bg-accent/15 text-accent'
    : 'text-muted hover:bg-elevated hover:text-secondary'
  }`

  return (
    <div className="h-full min-h-0 min-w-0 grid grid-cols-1 xl:grid-cols-[18rem_minmax(0,1fr)] gap-3">
      <section className="panel flex flex-col min-h-0 xl:overflow-y-auto">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Parameters</div>
            <h2 className="section-title">因子配置</h2>
          </div>
        </div>
        <div className="panel-body space-y-3">
          <p className="text-[11px] leading-4 text-muted">选择因子、区间和分组方式。默认最近 3 个月。</p>

          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">因子</label>
            <select
              value={factorName}
              onChange={e => setFactorName(e.target.value)}
              className={INPUT_CLS}
            >
              {Object.entries(factorGroups).map(([group, cols]) => (
                <optgroup key={group} label={group}>
                  {cols.map(c => (
                    <option key={c.id} value={c.id}>{c.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            {factorDesc && (
              <p className="mt-1 text-[11px] text-muted">{factorDesc}</p>
            )}
          </div>

          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">
              标的(逗号分隔，留空=全市场)
            </label>
            <input
              type="text"
              value={symbols}
              onChange={e => setSymbols(e.target.value)}
              placeholder="留空则使用全市场，建议最近3个月"
              className={`${INPUT_CLS} font-mono`}
            />
            <InstrumentSearchAdder
              onAdd={result => setSymbols(previous => appendUniqueSymbol(previous, result.symbol))}
              assetTypes={['stock']}
              placeholder="搜索名称或拼音后添加"
              ariaLabel="添加因子回测标的"
              className="mt-2"
            />
          </div>

          <div className="rounded-btn border border-border bg-elevated/40 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs font-medium text-foreground">回测区间</div>
              <span className="shrink-0 rounded-btn border border-accent/25 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
                {rangeTitle}
              </span>
            </div>

            <div className="mt-2 grid grid-cols-2 gap-2">
              <div>
                <label className="text-[11px] text-secondary block mb-1">开始</label>
                <DatePicker
                  value={start}
                  onChange={setStart}
                  max={end || undefined}
                  placeholder="全部历史"
                  className="w-full"
                  buttonClassName="w-full justify-start"
                  align="left"
                />
              </div>
              <div>
                <label className="text-[11px] text-secondary block mb-1">结束</label>
                <DatePicker
                  value={end}
                  onChange={setEnd}
                  min={start || undefined}
                  className="w-full"
                  buttonClassName="w-full justify-start"
                />
              </div>
            </div>

            <div className="mt-2 flex rounded-input bg-base p-0.5">
              <button type="button" onClick={() => applyRange(3)} className={`${rangeButtonCls('3m')} flex-1`}>3个月</button>
              <button type="button" onClick={() => applyRange(6)} className={`${rangeButtonCls('6m')} flex-1`}>6个月</button>
              <button type="button" onClick={() => applyRange(12)} className={`${rangeButtonCls('1y')} flex-1`}>1年</button>
              <button type="button" onClick={applyAllRange} className={`${rangeButtonCls('all')} flex-1`}>全部</button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">分组数</label>
              <select value={nGroups} onChange={e => setNGroups(Number(e.target.value))} className={INPUT_CLS}>
                <option value={3}>3组</option>
                <option value={5}>5组</option>
                <option value={10}>10组</option>
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">权重</label>
              <select value={weight} onChange={e => setWeight(e.target.value as 'equal' | 'factor_weight')} className={INPUT_CLS}>
                <option value="equal">等权</option>
                <option value="factor_weight">因子加权</option>
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-secondary block mb-1.5">佣金(万分之)</label>
              <input type="number" value={fees} onChange={e => setFees(e.target.value)}
                className={INPUT_CLS} />
            </div>
          </div>

          <button
            onClick={() => run.mutate()}
            disabled={run.isPending}
            className="btn-primary w-full"
          >
            <Play className="h-3.5 w-3.5" />
            {run.isPending ? '分析中…' : '开始因子分析'}
          </button>
        </div>
      </section>

      <section className="panel flex flex-col min-h-0 min-w-0 xl:overflow-y-auto">
        <div className="panel-header">
          <div>
            <div className="section-kicker">Results</div>
            <h2 className="section-title">分析结果</h2>
          </div>
        </div>
        <div className="panel-body space-y-3">
        {result?.error && !result.ic_mean && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {result.error}
          </div>
        )}

        {run.isError && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {String((run.error as Error).message)}
          </div>
        )}

        {!result && !run.isPending && (
          <EmptyState
            icon={BarChart3}
            title="选择因子并开始分析"
            hint="因子回测分析因子的预测能力 ( IC/IR ) 和分层收益差异。服务器建议优先使用最近3个月；长周期建议本机或 8GB 以上内存环境运行。"
          />
        )}

        {run.isPending && result && (
          <div className="rounded-btn border border-border bg-elevated px-3 py-2 text-xs text-secondary">
            正在重新计算，当前暂时展示上一次因子分析结果，完成后会自动替换。
          </div>
        )}

        {run.isPending && !result && (
          <LoadingPanel symbolsText={symbols ? `${symbols.split(',').length} 只标的` : '全市场 · 当前区间'} />
        )}

        {result && result.ic_mean != null && (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-3"
          >
            <div className="rounded-btn border border-border bg-elevated/30 p-3">
              <div className="flex items-center justify-between mb-3">
                <h3 className="section-title">因子预测能力</h3>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted">
                    Rank IC · 日度调仓
                  </span>
                  {result.elapsed_ms > 0 && (
                    <span className="flex items-center gap-1 text-[11px] text-muted">
                      <Clock className="h-3 w-3" />
                      <span className="num">{result.elapsed_ms.toFixed(0)} ms</span>
                    </span>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <StatCard
                  label="IC 均值"
                  value={result.ic_mean != null ? fmtPct(result.ic_mean) : null}
                  highlight={result.ic_mean != null
                    ? result.ic_mean > 0.03 ? 'bull' : result.ic_mean < -0.03 ? 'bear' : 'neutral'
                    : undefined}
                />
                <StatCard label="IC 标准差" value={result.ic_std != null ? fmtPct(result.ic_std) : null} />
                <StatCard
                  label="ICIR"
                  value={result.ir != null ? result.ir.toFixed(2) : null}
                  highlight={result.ir != null
                    ? Math.abs(result.ir) > 0.5 ? (result.ir > 0 ? 'bull' : 'bear') : 'neutral'
                    : undefined}
                />
                <StatCard label="IC 胜率" value={result.ic_win_rate != null ? fmtPct(result.ic_win_rate) : null} />
              </div>
            </div>

            {result.ic_series.length > 0 && (
              <div className="rounded-btn border border-border overflow-hidden">
                <div className="border-b border-border px-3 py-2">
                  <span className="text-xs font-medium text-secondary">IC 时序</span>
                </div>
                <div className="p-2">
                  <FactorICChart result={result} />
                </div>
              </div>
            )}

            {result.group_nav.length > 0 && (
              <div className="rounded-btn border border-border overflow-hidden">
                <div className="border-b border-border px-3 py-2">
                  <span className="text-xs font-medium text-secondary">分层净值曲线</span>
                </div>
                <div className="p-2">
                  <FactorGroupNavChart result={result} />
                </div>
              </div>
            )}

            {result.group_stats.length > 0 && (
              <div className="data-table-scroll rounded-btn border border-border overflow-hidden">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>分组</th>
                      <th className="text-right">总收益</th>
                      <th className="text-right">年化</th>
                      <th className="text-right">最大回撤</th>
                      <th className="text-right">夏普</th>
                      <th className="text-right">胜率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.group_stats.map((g: GroupStat) => (
                      <tr key={g.group}>
                        <td className="font-medium">{g.label}</td>
                        <td className={`text-right num ${priceColorClass(g.total_return)}`}>
                          {fmtPct(g.total_return)}
                        </td>
                        <td className={`text-right num ${priceColorClass(g.annual_return)}`}>
                          {fmtPct(g.annual_return)}
                        </td>
                        <td className="text-right num text-bear">{fmtPct(g.max_drawdown)}</td>
                        <td className="text-right num">{g.sharpe?.toFixed(2)}</td>
                        <td className="text-right num">{fmtPct(g.win_rate)}</td>
                      </tr>
                    ))}
                    {result.long_short_stats?.total_return != null && (
                      <tr className="bg-elevated/40">
                        <td className="font-medium text-accent">
                          多空({result.long_short_stats.top_group ?? ''}-{result.long_short_stats.bottom_group ?? ''})
                        </td>
                        <td className={`text-right num font-medium ${priceColorClass(result.long_short_stats.total_return)}`}>
                          {fmtPct(result.long_short_stats.total_return as number)}
                        </td>
                        <td className="text-right num">—</td>
                        <td className="text-right num text-bear">
                          {fmtPct(result.long_short_stats.max_drawdown as number)}
                        </td>
                        <td className="text-right num">—</td>
                        <td className="text-right num">—</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-4 text-[11px] text-muted">
              <span className="num">{result.n_symbols} 只标的</span>
              <span className="num">{result.n_dates} 个交易日</span>
              <span className="font-mono">run_id: {result.run_id}</span>
            </div>
          </motion.div>
        )}
        </div>
      </section>
    </div>
  )
}
