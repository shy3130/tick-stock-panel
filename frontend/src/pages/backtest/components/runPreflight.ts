/**
 * 运行前预检（纯前端，不发请求）。
 *
 * 在点击“运行回测”前对区间长度、股票池、成本假设与成交口径做启发式检查，
 * 提示常见的方法论风险。所有函数均为纯函数，可独立测试。
 */

export type PreflightLevel = 'warn' | 'info'

export interface PreflightFinding {
  key: string
  level: PreflightLevel
  message: string
}

export interface PreflightInput {
  /** 起始日 YYYY-MM-DD；空串 = 全部历史（无法估计样本长度） */
  start: string
  /** 结束日 YYYY-MM-DD */
  end: string
  /** 已解析的股票池（空数组 = 全市场） */
  symbols: string[]
  /** 佣金（万分之） */
  fees: number
  /** 滑点（bp） */
  slippage: number
  /** 建仓口径 */
  entryFill: 'close_t' | 'open_t+1'
  /** 上市天数门控；前端当前无法设置，恒为 0 */
  minListedDays?: number
}

/** 样本过短阈值：低于该交易日数的区间统计指标不稳定 */
export const MIN_SAMPLE_TRADING_DAYS = 250

const DAY_MS = 86_400_000

/**
 * 估算区间交易日数：按周一至周五计数（不含节假日），
 * 是交易日数的上界估计，仅用于样本长度启发式。
 * 日期不可解析或区间倒置时返回 null。
 */
export function estimateTradingDays(start: string, end: string): number | null {
  const s = Date.parse(start)
  const e = Date.parse(end)
  if (!Number.isFinite(s) || !Number.isFinite(e) || e < s) return null
  const days = Math.floor((e - s) / DAY_MS) + 1
  if (days <= 0) return null
  const fullWeeks = Math.floor(days / 7)
  const remainder = days % 7
  const startDow = new Date(s).getUTCDay()
  let count = fullWeeks * 5
  for (let i = 0; i < remainder; i += 1) {
    const dow = (startDow + i) % 7
    if (dow !== 0 && dow !== 6) count += 1
  }
  return count
}

export function buildPreflightFindings(input: PreflightInput): PreflightFinding[] {
  const findings: PreflightFinding[] = []
  const tradingDays = estimateTradingDays(input.start, input.end)
  if (tradingDays != null && tradingDays < MIN_SAMPLE_TRADING_DAYS) {
    findings.push({
      key: 'sample_short',
      level: 'warn',
      message: `区间约 ${tradingDays} 个交易日（不足 ${MIN_SAMPLE_TRADING_DAYS}）：样本过短，年化与风险调整指标不稳定，结论仅供参考。`,
    })
  }
  const fullMarket = input.symbols.length === 0
  if (fullMarket) {
    findings.push({
      key: 'survivorship_bias',
      level: 'warn',
      message: '未限定股票池，按全市场回测：以上市至今的成分近似历史全市场，存在幸存者偏差（已退市标的缺失）。',
    })
  }
  if (!(input.fees > 0) || !(input.slippage > 0)) {
    findings.push({
      key: 'zero_cost',
      level: 'warn',
      message: '佣金或滑点为 0：结果未计入交易成本，收益系统性偏乐观。',
    })
  }
  if (input.entryFill === 'close_t') {
    findings.push({
      key: 'close_t_entry',
      level: 'info',
      message: '建仓口径为信号日收盘成交（close_t）：假设当日收盘仍可成交，存在前视风险；如无特殊理由建议次日开盘（open_t+1）。',
    })
  }
  if (fullMarket && !(Number(input.minListedDays ?? 0) > 0)) {
    findings.push({
      key: 'young_listings',
      level: 'warn',
      message: '全市场且未启用上市天数门控（min_listed_days=0）：次新股（上市初期高波动、无涨跌幅限制阶段）会进入样本，可能推高波动类指标。',
    })
  }
  return findings
}
