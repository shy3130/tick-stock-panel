import type { ParamVisibleIf, StrategyParamDef } from '@/lib/api'

/**
 * 策略参数级联显隐与分组（META 里的 visible_if/group 为 UI 元数据，后端原样透传）。
 *
 * - visible_if 支持递归条件: { param, in } 单条件 / { all: [...] } 与 / { any: [...] } 或；
 *   values 缺省时回落该参数的 default（未触碰过的控件按默认值判定）。
 * - group: 相同组名的相邻声明归并为一节；无 group 的参数归入无名节（不显示节标题）。
 */
function evalVisibleIf(
  cond: ParamVisibleIf,
  values: Record<string, any>,
  allParams?: StrategyParamDef[],
): boolean {
  // 空集合语义: all:[] 按空真(vacuous true)处理; any:[] 无候选可成立 → false
  if (cond.all !== undefined) return cond.all.every(c => evalVisibleIf(c, values, allParams))
  if (cond.any !== undefined) return cond.any.length > 0 && cond.any.some(c => evalVisibleIf(c, values, allParams))
  if (cond.param) {
    const actual = values[cond.param] ?? allParams?.find(x => x.id === cond.param)?.default
    const expected = cond.in ?? []
    if (typeof actual === 'boolean') {
      // 容错: META 里 bool 条件的 in 值误写字符串 "true"/"false" 也能匹配
      return expected.some(e => e === actual || String(e).toLowerCase() === String(actual))
    }
    return expected.includes(actual)
  }
  return true
}

export function isParamVisible(
  p: StrategyParamDef,
  values: Record<string, any>,
  allParams?: StrategyParamDef[],
): boolean {
  if (!p.visible_if) return true
  return evalVisibleIf(p.visible_if, values, allParams)
}

export function visibleParams(
  params: StrategyParamDef[],
  values: Record<string, any>,
): StrategyParamDef[] {
  return params.filter(p => isParamVisible(p, values, params))
}

export interface ParamGroup {
  name: string | null
  items: StrategyParamDef[]
}

/**
 * 带分组前缀的展示标签。分组内短标签（如「止盈」「入池」）脱离分组上下文后
 * 不可区分（扫描面板是平铺列表），限定为「组名 · 标签」。
 */
export function paramDisplayLabel(p: StrategyParamDef): string {
  return p.group ? `${p.group} · ${p.label}` : p.label
}

export function groupParams(params: StrategyParamDef[]): ParamGroup[] {
  const groups: ParamGroup[] = []
  for (const p of params) {
    const name = p.group ?? null
    const last = groups[groups.length - 1]
    if (last && last.name === name) last.items.push(p)
    else groups.push({ name, items: [p] })
  }
  return groups
}
