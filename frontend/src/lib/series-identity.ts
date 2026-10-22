// 序列引用收敛 (盘中实时行情渲染优化)。
//
// 实时行情每 tick 都会重建派生序列 (当日蜡烛修补 / 分钟尾部续画), 某只标的数据
// 完全没变也会拿到新数组引用 — 下游迷你图 (MiniIntraday/MiniCandlestick) 的
// React.memo 因此每 tick 全量失效, 几千个 SVG 节点白白重建。这里在派生出口做
// 一次「内容未变沿用旧引用」的收敛。
//
// 比较是全字段浅比较, 刻意不维护字段白名单 — 任何字段变化都会产生新数组,
// 从构造上杜绝「漏比较一个字段导致画面停在旧价」的事故。

/** 两个 bar 对象是否全字段相等 (own key 集合一致且每个值 ===)。 */
export function sameBar(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  if (a === b) return true
  const ka = Object.keys(a)
  const kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  for (const k of ka) if (a[k] !== b[k]) return false
  return true
}

/** 两条 bar 序列是否逐根全等 (长度一致 + 每根全字段相等)。 */
export function sameSeries(
  a: readonly unknown[] | undefined,
  b: readonly unknown[] | undefined,
): boolean {
  if (a === b) return true
  if (!a || !b || a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    if (!sameBar(a[i] as Record<string, unknown>, b[i] as Record<string, unknown>)) return false
  }
  return true
}

/** 共享空序列: 代替 `xx ?? []`, 避免每次渲染新建数组击穿下游 memo。 */
export const EMPTY_ROWS: never[] = []

/** 以旧映射为参照收敛新映射: 逐 symbol 内容未变时把新数组换回旧引用 (原地改
 * next, 不动 prev)。返回值即 next。next 必须是调用方自建对象 — 会返回 base
 * 原对象的外来引用 (如 react-query 缓存数据) 不得传入, 否则缓存被原地改写。 */
export function reuseUnchangedSeries<T>(
  prev: Record<string, T[]>,
  next: Record<string, T[]>,
): Record<string, T[]> {
  for (const sym of Object.keys(next)) {
    const p = prev[sym]
    if (p && p !== next[sym] && sameSeries(p, next[sym])) next[sym] = p
  }
  return next
}
