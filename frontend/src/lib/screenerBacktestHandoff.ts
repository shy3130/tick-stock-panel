export type ScreenerBacktestTarget = 'factor' | 'strategy'

export interface ScreenerBacktestHandoff {
  target: ScreenerBacktestTarget
  symbols: string[]
  /** 筛选数据的有效交易日；回测仅可从该日开始。 */
  asOf: string | null
  /** 目标为策略回测时可选携带的策略 ID；缺省/空表示不指定策略。 */
  strategyId?: string | null
}

/** 筛选结果行的最小形状：带 `_expired` 标记的行是"今日已失效"的灰色行。 */
export type ActiveScreenerRow = { _expired?: boolean }

/**
 * 过滤掉今日已失效 (`_expired`) 的行。批量加自选 / 送回测只应使用仍有效的标的，
 * 失效行仅供页面内展示参考。
 */
export function filterActiveScreenerRows<T extends ActiveScreenerRow>(rows: readonly T[]): T[] {
  return rows.filter(row => !row._expired)
}

const STORAGE_KEY = 'condition-screener-backtest-handoff'
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

function session(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function normalizeSymbols(values: readonly unknown[]): string[] {
  const unique = new Set<string>()
  for (const value of values) {
    if (typeof value !== 'string') continue
    const symbol = value.trim().toUpperCase()
    if (symbol) unique.add(symbol)
  }
  return [...unique].slice(0, 500)
}

function normalizeAsOf(value: unknown): string | null {
  return typeof value === 'string' && DATE_RE.test(value) ? value : null
}

function normalizeStrategyId(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/**
 * 暂存一次性跨页交接。使用 sessionStorage，避免将瞬态筛选结果混入用户的长期回测偏好。
 */
export function stageScreenerBacktestHandoff(
  handoff: Omit<ScreenerBacktestHandoff, 'symbols'> & { symbols: readonly unknown[] },
): number {
  const storage = session()
  const symbols = normalizeSymbols(handoff.symbols)
  const strategyId = normalizeStrategyId(handoff.strategyId)
  // 纯策略交接: 空 symbols + 任意非空 strategyId (含 AI 生成 id) 仍持久化 —
  // 策略自身在回测区间内逐日选股, 不依赖当日筛选结果池。
  // 收紧不变式: 空 symbols 且无 strategyId 依旧拒绝, 避免把用户静默切到「全市场 + 默认策略」。
  if (!storage || (symbols.length === 0 && !strategyId)) return 0

  storage.setItem(STORAGE_KEY, JSON.stringify({
    target: handoff.target,
    symbols,
    asOf: normalizeAsOf(handoff.asOf),
    // 仅在显式给出非空策略 ID 时写入，旧 payload（无该字段）保持原样可读
    ...(strategyId ? { strategyId } : {}),
  } satisfies ScreenerBacktestHandoff))
  return symbols.length
}

/** 无副作用地读取交接内容；可安全用于 React 严格模式的 state initializer。 */
export function peekScreenerBacktestHandoff(): ScreenerBacktestHandoff | null {
  const storage = session()
  if (!storage) return null

  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (!raw) return null

    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object') return null
    const candidate = value as Partial<ScreenerBacktestHandoff>
    if (candidate.target !== 'factor' && candidate.target !== 'strategy') return null
    if (!Array.isArray(candidate.symbols)) return null

    const symbols = normalizeSymbols(candidate.symbols)
    const strategyId = normalizeStrategyId(candidate.strategyId)
    // 纯策略交接无股票池 — 有任意非空 strategyId 时允许空 symbols
    if (symbols.length === 0 && !strategyId) return null
    return {
      target: candidate.target,
      symbols,
      asOf: normalizeAsOf(candidate.asOf),
      strategyId,
    }
  } catch {
    return null
  }
}

/** 在目标页面已提交初始化状态后删除交接，确保不会在后续路由重入时重复带入。 */
export function clearScreenerBacktestHandoff(): void {
  session()?.removeItem(STORAGE_KEY)
}
