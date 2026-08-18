export type ScreenerBacktestTarget = 'factor' | 'strategy'

export interface ScreenerBacktestHandoff {
  target: ScreenerBacktestTarget
  symbols: string[]
  /** 筛选数据的有效交易日；回测仅可从该日开始。 */
  asOf: string | null
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

/**
 * 暂存一次性跨页交接。使用 sessionStorage，避免将瞬态筛选结果混入用户的长期回测偏好。
 */
export function stageScreenerBacktestHandoff(
  handoff: Omit<ScreenerBacktestHandoff, 'symbols'> & { symbols: readonly unknown[] },
): number {
  const storage = session()
  const symbols = normalizeSymbols(handoff.symbols)
  if (!storage || symbols.length === 0) return 0

  storage.setItem(STORAGE_KEY, JSON.stringify({
    target: handoff.target,
    symbols,
    asOf: normalizeAsOf(handoff.asOf),
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
    if (symbols.length === 0) return null
    return { target: candidate.target, symbols, asOf: normalizeAsOf(candidate.asOf) }
  } catch {
    return null
  }
}

/** 在目标页面已提交初始化状态后删除交接，确保不会在后续路由重入时重复带入。 */
export function clearScreenerBacktestHandoff(): void {
  session()?.removeItem(STORAGE_KEY)
}
