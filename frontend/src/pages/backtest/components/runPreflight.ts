/**
 * 运行前预检（纯前端，不发请求）。
 *
 * 在点击“运行回测”前对区间长度、股票池、成本假设与成交口径做启发式检查，
 * 提示常见的方法论风险。所有函数均为纯函数，可独立测试。
 */

export type PreflightLevel = 'warn' | 'info'

export interface PreflightFix {
  /** 按钮文案，如「拉长区间到约 250 个交易日」 */
  label: string
  /**
   * 待应用的表单补丁：键 = 表单字段名（start / end / fees_pct / slippage_bps / stamp_tax_pct）。
   * 数值口径：fees_pct、stamp_tax_pct 为小数（0.0002 = 万分之二），
   * slippage_bps 为 bp 数（5 = 万分之五），start / end 为 YYYY-MM-DD 字符串。
   * 应用端对没有对应输入项的键自动跳过（表单尚未提供该字段时不清空、不报错）。
   */
  patch: Record<string, unknown>
}

export interface PreflightFinding {
  key: string
  level: PreflightLevel
  message: string
  /** 可安全自动修复时提供；幸存者偏差、close_t 前视、次新股等方法论问题不给 fix */
  fix?: PreflightFix
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
  /** 预热天数（指标预热消耗的交易日数）；缺省按 120 估算 */
  warmupDays?: number
  /** 印花税（万分之，卖出单边）；未提供时不参与零成本判定 */
  stampTax?: number
}

/** 样本过短阈值：低于该交易日数的区间统计指标不稳定 */
export const MIN_SAMPLE_TRADING_DAYS = 250

/** 默认预热天数：与 WarmupBadge 口径一致（约 120 个交易日） */
export const DEFAULT_WARMUP_TRADING_DAYS = 120

/** 一键修复默认成本：佣金万分之二 / 滑点万分之五 / 印花税万分之五（卖出单边） */
export const DEFAULT_FEES_PCT = 0.0002
export const DEFAULT_SLIPPAGE_BPS = 5
export const DEFAULT_STAMP_TAX_PCT = 0.0005

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

/**
 * 建议的回测起始日：让「扣除预热后」的有效样本仍不少于最少交易日数。
 * 按 5/7 比例把交易日换算回日历日并加 2 天周末松弛；结束日不可解析时返回 null。
 */
export function suggestSampleStart(end: string, warmupDays: number): string | null {
  const e = Date.parse(end)
  if (!Number.isFinite(e)) return null
  const totalTradingDays = MIN_SAMPLE_TRADING_DAYS + Math.max(0, warmupDays)
  const calendarDays = Math.ceil((totalTradingDays / 5) * 7) + 2
  return new Date(e - calendarDays * DAY_MS).toISOString().slice(0, 10)
}

/**
 * 应用一键修复补丁到表单：只处理 appliers 里登记了的键，
 * 表单缺失对应输入项时静默跳过（不抛错、不清空）。
 * 返回实际应用成功的键列表（测试与调用方可据此判断补丁是否被完整消费）。
 */
export function applyPreflightPatch(
  patch: Record<string, unknown>,
  appliers: Record<string, (value: unknown) => void>,
): string[] {
  const applied: string[] = []
  for (const key of Object.keys(patch)) {
    const apply = appliers[key]
    if (typeof apply !== 'function') continue
    apply(patch[key])
    applied.push(key)
  }
  return applied
}

export function buildPreflightFindings(input: PreflightInput): PreflightFinding[] {
  const findings: PreflightFinding[] = []
  const tradingDays = estimateTradingDays(input.start, input.end)
  if (tradingDays != null && tradingDays < MIN_SAMPLE_TRADING_DAYS) {
    const finding: PreflightFinding = {
      key: 'sample_short',
      level: 'warn',
      message: `区间约 ${tradingDays} 个交易日（不足 ${MIN_SAMPLE_TRADING_DAYS}）：样本过短，年化与风险调整指标不稳定，结论仅供参考。`,
    }
    // 修复 = 把起始日拉长到「预热 + 最少样本」；建议值自身仍不达标时不给 fix（fail-closed）
    const warmupDays = Math.max(0, input.warmupDays ?? DEFAULT_WARMUP_TRADING_DAYS)
    const suggestedStart = suggestSampleStart(input.end, warmupDays)
    if (suggestedStart != null
      && (estimateTradingDays(suggestedStart, input.end) ?? 0) >= MIN_SAMPLE_TRADING_DAYS) {
      finding.fix = {
        label: `拉长区间到约 ${MIN_SAMPLE_TRADING_DAYS + warmupDays} 个交易日`,
        patch: { start: suggestedStart },
      }
    }
    findings.push(finding)
  }
  const fullMarket = input.symbols.length === 0
  if (fullMarket) {
    findings.push({
      key: 'survivorship_bias',
      level: 'warn',
      message: '未限定股票池，按全市场回测：以上市至今的成分近似历史全市场，存在幸存者偏差（已退市标的缺失）。',
    })
  }
  const feesMissing = !(input.fees > 0)
  const slippageMissing = !(input.slippage > 0)
  const stampMissing = input.stampTax != null && !(input.stampTax > 0)
  if (feesMissing || slippageMissing || stampMissing) {
    const finding: PreflightFinding = {
      key: 'zero_cost',
      level: 'warn',
      message: '佣金 / 滑点 / 印花税存在 0 或未设置：结果未计入交易成本，收益系统性偏乐观。',
    }
    // 修复 = 只填缺失项的默认成本；已设置的不覆盖，表单缺字段的键应用时跳过
    const patch: Record<string, unknown> = {}
    if (feesMissing) patch.fees_pct = DEFAULT_FEES_PCT
    if (slippageMissing) patch.slippage_bps = DEFAULT_SLIPPAGE_BPS
    if (stampMissing) patch.stamp_tax_pct = DEFAULT_STAMP_TAX_PCT
    finding.fix = { label: '填入默认成本（佣金万2 · 滑点万5 · 印花税万5）', patch }
    findings.push(finding)
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
