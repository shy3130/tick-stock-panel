export type MetricTone = 'positive' | 'negative' | 'warning' | 'neutral'
export type MetricSeverity = 'danger' | 'warning' | 'info'
export type MetricFormat = 'pct' | 'number' | 'ratio' | 'int' | 'days'

export interface MetricDefinition {
  key: string
  label: string
  format: MetricFormat
  group: 'return' | 'risk' | 'quality' | 'execution'
  description: string
}

export interface MetricFinding {
  key: string
  title: string
  detail: string
  severity: MetricSeverity
}

export const METRIC_DEFINITIONS: MetricDefinition[] = [
  { key: 'total_return', label: '总收益', format: 'pct', group: 'return', description: '区间累计净值收益' },
  { key: 'annual_return', label: '年化收益', format: 'pct', group: 'return', description: '按指标上下文频率年化' },
  { key: 'benchmark_return', label: '基准收益', format: 'pct', group: 'return', description: '同期基准累计收益' },
  { key: 'excess', label: '超额收益', format: 'pct', group: 'return', description: '策略收益减基准收益' },
  { key: 'alpha', label: 'Alpha', format: 'number', group: 'return', description: '相对基准的年化截距' },
  { key: 'sharpe', label: '夏普比率', format: 'ratio', group: 'quality', description: '超额收益相对总体波动' },
  { key: 'sortino', label: 'Sortino', format: 'ratio', group: 'quality', description: '超额收益相对下行波动' },
  { key: 'calmar', label: 'Calmar', format: 'ratio', group: 'quality', description: '年化收益相对最大回撤' },
  { key: 'information_ratio', label: '信息比率', format: 'ratio', group: 'quality', description: '主动收益相对跟踪误差' },
  { key: 'omega', label: 'Omega', format: 'ratio', group: 'quality', description: '阈值以上收益与以下损失之比' },
  { key: 'profit_factor', label: '利润因子', format: 'ratio', group: 'quality', description: '总盈利与总亏损绝对值之比' },
  { key: 'payoff_ratio', label: '盈亏比', format: 'ratio', group: 'quality', description: '平均盈利与平均亏损绝对值之比' },
  { key: 'tail_ratio', label: '尾部比率', format: 'ratio', group: 'quality', description: '右尾与左尾损失幅度之比' },
  { key: 'max_drawdown', label: '最大回撤', format: 'pct', group: 'risk', description: '净值相对历史峰值的最大跌幅' },
  { key: 'annual_volatility', label: '年化波动', format: 'pct', group: 'risk', description: '收益率标准差的年化值' },
  { key: 'downside_deviation', label: '下行波动', format: 'pct', group: 'risk', description: '低于最低可接受收益的波动' },
  { key: 'tracking_error', label: '跟踪误差', format: 'pct', group: 'risk', description: '主动收益波动的年化值' },
  { key: 'ulcer_index', label: 'Ulcer Index', format: 'number', group: 'risk', description: '回撤深度与持续性的均方根' },
  { key: 'recovery_factor', label: '恢复因子', format: 'ratio', group: 'risk', description: '累计收益相对最大回撤' },
  { key: 'win_rate', label: '胜率', format: 'pct', group: 'quality', description: '盈利交易占已平仓交易比例' },
  { key: 'avg_turnover', label: '平均换手', format: 'pct', group: 'execution', description: '每个调仓期的平均换手率' },
  { key: 'total_turnover', label: '累计换手', format: 'pct', group: 'execution', description: '全区间换手率之和' },
  { key: 'max_exposure', label: '最大敞口', format: 'pct', group: 'execution', description: '组合历史最大资金暴露' },
  { key: 'avg_duration', label: '平均持仓', format: 'days', group: 'execution', description: '已平仓交易平均持有天数' },
  { key: 'n_trades', label: '交易数', format: 'int', group: 'execution', description: '参与统计的交易笔数' },
  { key: 'pending_exit_positions', label: '未完成退出', format: 'int', group: 'execution', description: '回测结束仍未完成退出的持仓数' },
]

export function finiteMetric(value: unknown): number | null {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function metricTone(key: string, value: number): MetricTone {
  if (key === 'max_drawdown') return value <= -0.2 ? 'negative' : value <= -0.1 ? 'warning' : 'neutral'
  if (key === 'pending_exit_positions') return value > 0 ? 'negative' : 'positive'
  if (['total_return', 'annual_return', 'excess', 'alpha', 'sharpe', 'sortino', 'calmar', 'information_ratio'].includes(key)) {
    return value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral'
  }
  if (['profit_factor', 'payoff_ratio', 'tail_ratio', 'omega', 'recovery_factor'].includes(key)) {
    return value >= 1 ? 'positive' : 'warning'
  }
  return 'neutral'
}

export function buildMetricFindings(stats: Record<string, unknown> | null | undefined): MetricFinding[] {
  if (!stats) return []
  const findings: MetricFinding[] = []
  const value = (key: string) => finiteMetric(stats[key])
  const drawdown = value('max_drawdown')
  const sharpe = value('sharpe')
  const sortino = value('sortino')
  const profitFactor = value('profit_factor')
  const omega = value('omega')
  const trackingError = value('tracking_error')
  const exposure = value('max_exposure')
  const pending = value('pending_exit_positions')

  if (drawdown != null && drawdown <= -0.2) findings.push({ key: 'drawdown', title: '回撤压力较高', detail: `最大回撤 ${(Math.abs(drawdown) * 100).toFixed(1)}%，需要核对容量、止损和样本外稳定性。`, severity: 'danger' })
  else if (drawdown != null && drawdown <= -0.1) findings.push({ key: 'drawdown', title: '回撤需要关注', detail: `最大回撤 ${(Math.abs(drawdown) * 100).toFixed(1)}%，建议结合恢复期和滚动窗口检查。`, severity: 'warning' })
  if (sharpe != null && sharpe < 0) findings.push({ key: 'sharpe', title: '风险调整后收益为负', detail: `夏普比率 ${sharpe.toFixed(2)}，收益未覆盖当前无风险收益与波动成本。`, severity: 'danger' })
  else if (sharpe != null && sharpe < 1) findings.push({ key: 'sharpe', title: '夏普比率偏弱', detail: `夏普比率 ${sharpe.toFixed(2)}，尚不足以单独支持稳定性结论。`, severity: 'warning' })
  if (sortino != null && sortino < 0) findings.push({ key: 'sortino', title: '下行风险收益为负', detail: `Sortino ${sortino.toFixed(2)}，下行波动未获得正向补偿。`, severity: 'danger' })
  if (profitFactor != null && profitFactor < 1) findings.push({ key: 'profit_factor', title: '总亏损高于总盈利', detail: `利润因子 ${profitFactor.toFixed(2)}，盈利交易尚不能覆盖亏损交易。`, severity: 'danger' })
  if (omega != null && omega < 1) findings.push({ key: 'omega', title: 'Omega 低于 1', detail: `Omega ${omega.toFixed(2)}，阈值以下损失大于阈值以上收益。`, severity: 'warning' })
  if (trackingError != null && trackingError > 0.3) findings.push({ key: 'tracking_error', title: '基准偏离较大', detail: `年化跟踪误差 ${(trackingError * 100).toFixed(1)}%，策略表现可能与基准缺少可比性。`, severity: 'warning' })
  if (exposure != null && exposure > 1.0001) findings.push({ key: 'exposure', title: '敞口超过 100%', detail: `最大敞口 ${(exposure * 100).toFixed(1)}%，请确认杠杆和资金口径是否符合预期。`, severity: 'warning' })
  if (pending != null && pending > 0) findings.push({ key: 'pending_exit', title: '存在未完成退出', detail: `回测结束仍有 ${Math.round(pending)} 个持仓未完成退出，收益与交易统计可能不完整。`, severity: 'danger' })
  return findings
}
