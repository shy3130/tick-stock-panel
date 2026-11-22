import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { api } from '@/lib/api'
import type { EngineResearchReport } from '@/lib/engineJobTypes'
import { Button, ErrorNotice, inputClass } from './ui'
import { decimalLabel, downloadFile, downloadReport, list, priceLabel, record, text } from './viewModel'

function Metrics({ value }: { value: unknown }) {
  const metrics = record(value)
  const checkpoints = metrics.calculation === 'checkpoint_equity_decimal12_half_even'
  if (metrics.metrics_version !== '1.0' || (!checkpoints && metrics.calculation !== 'daily_equity_decimal12_half_even'))
    return <p className="text-sm text-secondary">此指标版本暂不支持展示，可以导出报告检查原始结果。</p>
  return (
    <dl className="grid gap-x-5 gap-y-1.5 text-sm sm:grid-cols-2">
      {[
        ['初始资金', `${decimalLabel(metrics.initial_capital)} ${text(metrics.currency, '')}`],
        ['期末权益', `${decimalLabel(metrics.final_equity)} ${text(metrics.currency, '')}`],
        ['区间收益率', decimalLabel(metrics.total_return, true)],
        ['最大回撤', decimalLabel(metrics.maximum_drawdown, true)],
        [checkpoints ? '成交委托数' : '成交笔数', typeof metrics.filled_orders === 'number' ? String(metrics.filled_orders) : '未提供'],
      ].map(([label, value]) => (
        <div
          key={label}
          className="flex min-w-0 flex-wrap items-baseline justify-between gap-2 border-b border-border/60 py-1.5"
        >
          <dt className="text-secondary">{label}</dt>
          <dd className="break-all font-mono text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Study({ summary }: { summary: Record<string, unknown> }) {
  const study = record(summary.study)
  const result = record(summary.report)
  const request = record(study.request)
  const windows = list(result.windows).map(record)
  const [windowIndex, setWindowIndex] = useState(0)
  const [page, setPage] = useState(0)
  if (
    study.plan_version !== '1.0' ||
    result.report_version !== '1.0' ||
    !['grid', 'walk_forward'].includes(text(request.mode))
  )
    return <ErrorNotice error="当前页面无法解释此研究报告版本，请导出报告查看。" />
  const selected = windows[windowIndex]
  if (!selected) return <p className="text-sm text-secondary">报告未包含可展示的研究窗口。</p>
  const window = record(list(study.windows)[windowIndex])
  const training = list(selected.training).map(record)
  const candidates = list(study.candidates).map(record)
  const winnerIndex = training.findIndex((trial) => trial.candidate_id === selected.winner_candidate_id)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <h4 className="font-medium">{request.mode === 'walk_forward' ? '滚动验证' : '参数搜索'}</h4>
          <label className="block text-sm text-secondary" htmlFor="study-window">
            研究窗口
          </label>
          <select
            id="study-window"
            className={inputClass}
            value={windowIndex}
            onChange={(event) => {
              setWindowIndex(Number(event.target.value))
              setPage(0)
            }}
          >
            {windows.map((_, index) => (
              <option value={index} key={index}>
                窗口 {index + 1}
              </option>
            ))}
          </select>
        </div>
        <p className="text-sm text-secondary">{training.length} 个候选 · 按训练期结果选择</p>
      </div>
      <p className="text-sm text-secondary">
        训练区间：{text(window.train_start)} 至 {text(window.train_end)}
      </p>
      {request.mode === 'walk_forward' && (
        <>
          <p className="text-sm text-secondary">
            样本外区间：{text(window.test_start)} 至 {text(window.test_end)}
          </p>
          {result.aggregate_method === 'independent_fold_return_product' ? (
            <p className="border-y border-border py-3 text-sm leading-6">
              各窗口重置账户。样本外收益按独立窗口收益连乘汇总：
              <strong className="font-mono">{decimalLabel(result.oos_compounded_return, true)}</strong>
              ，不代表连续持仓账户的收益。
            </p>
          ) : (
            <ErrorNotice error="报告使用了未知的汇总方式，未展示合并收益。" />
          )}
          <div className="space-y-2">
            <h4 className="text-sm font-medium">本窗口样本外结果</h4>
            <Metrics value={record(selected.test).metrics} />
          </div>
        </>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium">训练候选结果</h4>
        {winnerIndex >= 0 && (
          <Button onClick={() => setPage(Math.floor(winnerIndex / 25))}>查看选中候选</Button>
        )}
      </div>
      <div className="relative max-w-full overflow-x-auto rounded-btn border border-border">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">当前窗口的训练候选结果</caption>
          <thead className="bg-base text-secondary">
            <tr>
              {['候选', '收益率', '最大回撤', '成交笔数'].map((label) => (
                <th key={label} scope="col" className="whitespace-nowrap px-2.5 py-1.5 font-medium">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {training.slice(page * 25, (page + 1) * 25).map((trial) => {
              const metrics = record(trial.metrics)
              const id = text(trial.candidate_id, '')
              const winner = id === selected.winner_candidate_id
              const candidate = candidates.find((candidate) => candidate.candidate_id === id)
              const known =
                metrics.metrics_version === '1.0' &&
                metrics.calculation === 'daily_equity_decimal12_half_even'
              return (
                <tr key={id} className="border-t border-border align-top">
                  <th scope="row" className="px-2.5 py-1.5 font-normal">
                    <span className="font-mono">{id.slice(0, 10)}</span>
                    {winner && (
                      <span className="ml-2 whitespace-nowrap text-blue-700 dark:text-blue-400">训练选中</span>
                    )}
                    {candidate && (
                      <details className="mt-2 text-xs text-secondary">
                        <summary className="cursor-pointer py-1">查看参数</summary>
                        <pre className="mt-2 max-w-xs overflow-x-auto whitespace-pre-wrap break-all">
                          {JSON.stringify(candidate, null, 2)}
                        </pre>
                      </details>
                    )}
                  </th>
                  <td className="px-2.5 py-1.5 font-mono">
                    {known ? decimalLabel(metrics.total_return, true) : '未知口径'}
                  </td>
                  <td className="px-2.5 py-1.5 font-mono">
                    {known ? decimalLabel(metrics.maximum_drawdown, true) : '未知口径'}
                  </td>
                  <td className="px-3 py-3 font-mono">
                    {known && typeof metrics.filled_orders === 'number' ? metrics.filled_orders : '未提供'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {training.length > 25 && (
        <div className="flex items-center justify-between gap-3 text-sm">
          <Button disabled={page === 0} onClick={() => setPage(page - 1)}>
            上一页
          </Button>
          <span>
            {page + 1} / {Math.ceil(training.length / 25)}
          </span>
          <Button disabled={(page + 1) * 25 >= training.length} onClick={() => setPage(page + 1)}>
            下一页
          </Button>
        </div>
      )}
    </div>
  )
}

export function Report({ report }: { report: EngineResearchReport }) {
  const nodeId = report.node_id.replace(/\.report$/, '')
  const download = useMutation({
    mutationFn: async () => {
      const file = await api.engineJobResultZip(report.plan_id, nodeId)
      downloadFile(file, `tivelo-result-${report.execution.identity}.zip`)
    },
  })
  const summary = record(report.summary)
  const semantics = record(summary.semantics)
  const execution = record(semantics.execution)
  const prices = record(semantics.price_view)
  const account =
    semantics.accounting_model === 'event_ledger'
      ? '事件账本'
      : semantics.accounting_model === 'legacy_adjusted_curve'
        ? '旧版复权曲线'
        : '未知账户模型'
  return (
    <section aria-labelledby="research-report-title" className="space-y-3 border-t border-border pt-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 id="research-report-title" className="min-w-0 break-all text-lg font-semibold">
          研究报告 · {nodeId}
        </h3>
        <div className="flex flex-wrap gap-2">
          <Button variant="quiet" onClick={() => downloadReport(report)}>
            <Download aria-hidden="true" className="h-4 w-4" />
            导出报告 JSON
          </Button>
          <Button
            aria-busy={download.isPending}
            disabled={download.isPending}
            onClick={() => download.mutate()}
          >
            <Download aria-hidden="true" className="h-4 w-4" />
            {download.isPending ? '正在校验并打包' : '导出结果 ZIP'}
          </Button>
        </div>
      </div>
      {download.error && <ErrorNotice error={download.error} />}
      {report.format !== 'tsp-engine-report' || report.version !== '1.0' ? (
        <ErrorNotice error="报告版本不受当前页面支持，请导出原始报告。" />
      ) : (
        <>
          <dl className="grid gap-x-5 gap-y-1.5 border-y border-border py-2.5 text-sm sm:grid-cols-2">
            {[
              ['回测区间', `${text(semantics.start)} 至 ${text(semantics.end)}`],
              ['价格口径', priceLabel(prices.mode, prices.provider_id)],
              ['账户模型', account],
              [
                '成交时刻',
                execution.fill_at === 'next_open'
                  ? '下一交易时段开盘'
                  : execution.fill_at === 'legacy_same_close'
                    ? '旧版同日收盘'
                    : execution.fill_at === 'witnessed_trade'
                      ? '按逐笔报告确认'
                      : '未提供',
              ],
              [
                '信息要求',
                semantics.knowledge_requirement === 'historical_versions'
                  ? '按历史可知版本'
                  : semantics.knowledge_requirement === 'latest_reconstruction'
                    ? '按最新资料重建'
                    : '未提供',
              ],
              [
                '期末处理',
                execution.end_policy === 'mark_to_market'
                  ? '按市场价格估值'
                  : execution.end_policy === 'attempt_liquidation'
                    ? '尝试按规则平仓'
                    : '未提供',
              ],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0">
                <dt className="text-secondary">{label}</dt>
                <dd className="mt-1 break-words">{value}</dd>
              </div>
            ))}
          </dl>
          {summary.metrics ? (
            <Metrics value={summary.metrics} />
          ) : (
            <Study key={report.plan_id + report.node_id} summary={summary} />
          )}
          {list(summary.diagnostics).length > 0 && (
            <div className="space-y-2 text-sm">
              <h4 className="font-medium">运行诊断</h4>
              <ul className="list-inside list-disc space-y-1 text-secondary">
                {list(summary.diagnostics).map((item, index) => (
                  <li key={index} className="break-words">
                    {text(item)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <details className="border-t border-border pt-3 text-sm">
            <summary className="cursor-pointer py-2 text-secondary focus-visible:outline-accent">
              查看锁定输入与规则
            </summary>
            <dl className="mt-3 space-y-3">
              {[
                ['快照', semantics.snapshot_id],
                ['运行锁', semantics.lock_id],
                ['市场规则', record(semantics.market_profile).definition_id],
                ['策略', record(semantics.strategy).definition_id],
                ['价格锚点', prices.anchor_at],
              ].map(([label, value]) => (
                <div key={String(label)}>
                  <dt className="text-secondary">{String(label)}</dt>
                  <dd className="mt-1 break-all font-mono text-xs">{text(value)}</dd>
                </div>
              ))}
            </dl>
          </details>
        </>
      )}
    </section>
  )
}
