/**
 * 画布字号整体 +1 — ECharts 的文字画在 canvas 上, CSS 字号覆盖管不到,
 * 只能在 option 进 setOption 前改数值。配合 index.css「字号整体 +1 档」一节,
 * 让图表与 DOM 文字一起上调 1px。
 *
 * 实现要点:
 * - 只认 fontSize 这个数值键 (CSS/DOM 之外的唯一可靠信号), 深层 textStyle/rich 一并覆盖。
 * - 0~3px 视为「隐藏文字」(option 里用 fontSize: 0 关掉标签), 不动, 免得冒出 1px 灰点。
 * - 写时复制: 未改动的子树原样返回同一引用, 大行情数组 (纯数字) 走一次遍历即短路,
 *   不做整棵深拷贝 — setOption 在实时刷新里可能每秒多次。
 */
const MIN_VISIBLE_FONT = 4
const MAX_DEPTH = 24

type Dict = Record<string, unknown>

function isPlainObject(v: unknown): v is Dict {
  if (v === null || typeof v !== 'object') return false
  const proto = Object.getPrototypeOf(v)
  return proto === Object.prototype || proto === null
}

function walk(node: unknown, depth: number): unknown {
  if (node === null || typeof node !== 'object' || depth > MAX_DEPTH) return node

  if (Array.isArray(node)) {
    // 纯原始值数组 (series.data 等) 直接跳过, 避免每次 setOption 都重建大数组
    if (node.length > 0 && !node.some(item => item !== null && typeof item === 'object')) return node
    let changed = false
    const next = node.map(item => {
      const out = walk(item, depth + 1)
      if (out !== item) changed = true
      return out
    })
    return changed ? next : node
  }

  if (!isPlainObject(node)) return node

  let changed = false
  const next: Dict = {}
  for (const key of Object.keys(node)) {
    const value = node[key]
    if (key === 'fontSize' && typeof value === 'number') {
      const bumped = value >= MIN_VISIBLE_FONT ? value + 1 : value
      if (bumped !== value) changed = true
      next[key] = bumped
      continue
    }
    const out = walk(value, depth + 1)
    if (out !== value) changed = true
    next[key] = out
  }
  return changed ? next : node
}

/** 返回字号 +1 后的新 option (原对象不变; 无字号时不产生新对象)。 */
export function bumpChartFonts<T>(option: T): T {
  return walk(option, 0) as T
}
