import { Download, ScanSearch } from 'lucide-react'
import type { BacktestEvidence } from '@/lib/api'

const STATUS_LABELS: Record<BacktestEvidence['lookahead']['status'], string> = {
  passed: '抽样未发现差异', failed: '检测到历史输出变化', partial: '部分检查未完成',
  error: '检查未完成', insufficient: '数据不足以检查', cancelled: '检查已取消',
}
const DATA_LABELS: Record<string, string> = {
  daily_base: '日线基础数据', daily_matrix: '日线行情与输入字段',
  instruments: '标的信息', historical_shares: '历史股本',
  financial_announcements: '财务公告', regime_history: '市场环境', benchmark: '同期基准',
}
const FIELD_LABELS: Record<string, string> = {
  entry: '买入信号', exit: '卖出信号', score: '评分',
  _audit_candidate: '候选标的', _audit_entry: '买入信号', _audit_exit: '卖出信号',
  signal_presence: '信号出现时间', entry_signal_code: '买入触发器', exit_signal_code: '卖出触发器',
}
const CHECK_LABELS: Record<string, string> = { truncate: '截断未来', perturb: '扰动未来', repeat: '基线复算' }

function exportEvidence(evidence: BacktestEvidence) {
  const blob = new Blob([JSON.stringify(evidence, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `backtest-evidence-${evidence.run_id}.json`
  anchor.click()
  URL.revokeObjectURL(url)
}

export function BacktestEvidencePanel({ evidence }: { evidence?: BacktestEvidence | null }) {
  if (!evidence) {
    return <p className="text-xs text-muted">此结果没有检查与版本记录，重新回测后自动生成。</p>
  }
  const { lookahead } = evidence
  const completed = lookahead.checks.filter(check => check.status === 'completed').length
  const examples = lookahead.checks.flatMap(check => check.examples.map(example => ({ ...example, kind: check.kind }))).slice(0, 15)
  const failed = lookahead.status === 'failed'
  const datasets = Object.entries(evidence.data.datasets)
  const color = failed ? 'text-danger' : lookahead.status === 'passed' ? 'text-foreground' : 'text-warning'

  return (
    <section aria-label="回测检查与版本记录" className="min-w-0 rounded-card border border-border bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2 text-xs">
          <ScanSearch className="h-4 w-4 shrink-0 text-secondary" aria-hidden="true" />
          <span className="font-medium text-foreground">未来数据检查</span>
          <span className={color}>{STATUS_LABELS[lookahead.status]}</span>
        </div>
        <button type="button" onClick={() => exportEvidence(evidence)} className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-xs text-secondary hover:text-accent">
          <Download className="h-3 w-3" aria-hidden="true" />导出核查报告
        </button>
      </div>
      {failed && <p className="mt-2 text-xs leading-5 text-danger">改变未来数据后，部分历史输出发生变化。请结合下方差异排查策略或指标。</p>}
      {evidence.execution_timing?.status === 'failed' && (
        <p role="status" className="mt-2 text-xs leading-5 text-danger">信号与成交时间关系异常，共 {evidence.execution_timing.differences} 处。请查看导出报告中的时间核对记录。</p>
      )}
      {evidence.persistence.status === 'error' && <p role="status" className="mt-2 text-xs text-warning">版本记录未能保存，请导出本次报告。</p>}
      {evidence.strategy.changed_during_run && <p role="status" className="mt-2 text-xs text-warning">定义在回测期间发生变化，请重新回测确认。</p>}
      <details className="mt-2 text-xs text-secondary" open={failed || undefined}>
        <summary className="cursor-pointer py-1">检查与版本详情 · 完成 {completed}/{lookahead.checks.length} 项</summary>
        <div className="mt-3 space-y-3">
          <p className="leading-5">固定参数与预热起点，抽样截断、扰动未来数据并重算历史输出。检查结果仅覆盖本次样本和已加载的数据。</p>
          <dl className="grid grid-cols-1 gap-x-5 gap-y-2 sm:grid-cols-2">
            {[
              ['数据版本', evidence.data.sha256], ['策略版本', evidence.strategy.sha256],
              ['参数版本', evidence.config_sha256], ['回测编号', evidence.run_id],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0"><dt className="text-muted">{label}</dt><dd className="mt-0.5 break-all font-mono text-foreground" title={value}>{value.slice(0, 20)}</dd></div>
            ))}
          </dl>
          <p>程序版本 {evidence.runtime.application} · 记录时间 {new Date(evidence.created_at).toLocaleString('zh-CN')}</p>
          {evidence.execution_timing && (
            <p>信号与成交时间：{evidence.execution_timing.status === 'passed' ? `已核对 ${evidence.execution_timing.compared_values} 项` : evidence.execution_timing.status === 'failed' ? '存在异常' : '暂无可核对的已完成交易记录'}</p>
          )}
          {lookahead.checks.some(check => check.status === 'error') && (
            <ul className="space-y-1 text-warning">{lookahead.checks.filter(check => check.status === 'error').map((check, i) => <li key={i}>{check.cutoff} · {CHECK_LABELS[check.kind] ?? check.kind} · {check.error}</li>)}</ul>
          )}
          {examples.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full whitespace-nowrap text-left text-xs">
                <caption className="pb-2 text-left text-muted">差异示例（最多展示 15 条，完整抽样记录见导出报告）</caption>
                <thead><tr className="border-b border-border text-muted">{['历史时间 / 标的', '字段', '原值 → 重算值', '检查截点'].map(label => <th key={label} className="px-2 py-2 font-medium">{label}</th>)}</tr></thead>
                <tbody>{examples.map((row, i) => <tr key={i} className="border-b border-border/50"><td className="px-2 py-2">{row.date}<br />{row.symbol}</td><td className="px-2 py-2">{FIELD_LABELS[row.field] ?? row.field}</td><td className="px-2 py-2 font-mono">{String(row.baseline ?? '空')} → {String(row.recomputed ?? '空')}</td><td className="px-2 py-2">{row.cutoff}<br />{CHECK_LABELS[row.kind] ?? row.kind}</td></tr>)}</tbody>
              </table>
            </div>
          )}
          <div className="space-y-1">
            <p className="font-medium text-foreground">本次数据范围</p>
            {datasets.slice(0, 20).map(([name, data]) => (
              <p key={name} className="break-words">{DATA_LABELS[name] ?? (name.startsWith('minute_') ? '分钟数据 ' + name.split('/').slice(1).join(' · ') : name)} · {data.rows.toLocaleString()} 行{data.start && ` · ${data.start} ～ ${data.end}`}</p>
            ))}
            {datasets.length > 20 && <p className="text-muted">共 {datasets.length} 批数据，完整范围见导出报告。</p>}
          </div>
          <p className="leading-5 text-muted">版本记录保存内容指纹与有效参数。它不备份全量行情，也不验证供应商历史修订、历史股票池和静态维表是否符合当时可用的信息。</p>
        </div>
      </details>
    </section>
  )
}
