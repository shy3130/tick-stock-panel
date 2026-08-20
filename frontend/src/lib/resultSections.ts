import {
  BarChart2,
  Calculator,
  CloudSun,
  Coins,
  FlaskConical,
  LineChart,
  Palette,
  PieChart,
  ShieldCheck,
  Stethoscope,
  Table2,
  type LucideIcon,
} from 'lucide-react'

/**
 * 回测结果区区块注册表 + 折叠状态纯函数。
 *
 * - 注册表描述「有哪些结果区块、何时显示」，页面据此渲染锚点导航与 section 壳；
 * - 折叠状态是一个「被折叠区块 key 数组」，持久化在 storage.backtestResultSections；
 * - 本文件只做纯计算，不触碰 DOM / storage，方便单测。
 */

/** 结果分支: legacy-full=旧全量统计(历史缓存), candidate=候选独立执行, position=仓位模拟 */
export type ResultSectionVariant = 'legacy-full' | 'candidate' | 'position'

/** 判定区块是否渲染的上下文，由页面从 result 派生 */
export interface ResultSectionContext {
  variant: ResultSectionVariant
  hasEquityCurve: boolean
  hasReturnDistribution: boolean
  hasTrades: boolean
  hasAttribution: boolean
  hasRobustness: boolean
}

export interface ResultSectionDef {
  /** 区块锚点 key，同时是折叠记录 key */
  key: string
  /** 导航 chip 与 section 标题 */
  title: string
  /** 可选图标 */
  icon?: LucideIcon
  /** 何时渲染该区块 */
  when: (ctx: ResultSectionContext) => boolean
}
export const RESULT_SECTIONS: readonly ResultSectionDef[] = [
  {
    key: 'stats',
    title: '统计',
    icon: Calculator,
    when: () => true,
  },
  {
    key: 'nav_chart',
    title: '净值曲线',
    icon: LineChart,
    when: ctx => ctx.hasEquityCurve,
  },
  {
    key: 'professional',
    title: '专业诊断',
    icon: Stethoscope,
    when: ctx => ctx.variant === 'position',
  },
  {
    key: 'trust',
    title: '可信度诊断',
    icon: ShieldCheck,
    when: ctx => ctx.variant === 'position',
  },
  {
    key: 'attribution',
    title: '行业归因',
    icon: PieChart,
    when: ctx => ctx.variant !== 'legacy-full' && ctx.hasAttribution,
  },
  {
    key: 'robustness',
    title: '稳健性',
    icon: FlaskConical,
    when: ctx => ctx.variant === 'position' && ctx.hasRobustness,
  },
  {
    key: 'regime',
    title: '市场状态',
    icon: CloudSun,
    when: ctx => ctx.variant === 'position' && ctx.hasRobustness,
  },
  {
    key: 'cost_sensitivity',
    title: '成本敏感性',
    icon: Coins,
    when: ctx => ctx.variant === 'position' && ctx.hasRobustness,
  },
  {
    key: 'style',
    title: '风格归因',
    icon: Palette,
    when: ctx => ctx.variant === 'position' && ctx.hasRobustness,
  },
  {
    key: 'return_distribution',
    title: '收益分布',
    icon: BarChart2,
    when: ctx => ctx.hasReturnDistribution,
  },
  {
    key: 'trades',
    title: '交易明细',
    icon: Table2,
    when: ctx => ctx.variant !== 'legacy-full' && ctx.hasTrades,
  },
]

/** 按 key 取注册表定义；未知 key 直接抛错，避免静默渲染错壳 */
export function getResultSection(key: string): ResultSectionDef {
  const def = RESULT_SECTIONS.find(section => section.key === key)
  if (!def) throw new Error(`未知结果区块: ${key}`)
  return def
}

/** 当前上下文下实际渲染的区块（导航条只列出这些） */
export function visibleResultSections(ctx: ResultSectionContext): ResultSectionDef[] {
  return RESULT_SECTIONS.filter(section => section.when(ctx))
}

/** 折叠状态: 记录被折叠的区块 key 数组 */
export function toggleSection(collapsed: readonly string[], key: string): string[] {
  return collapsed.includes(key)
    ? collapsed.filter(item => item !== key)
    : [...collapsed, key]
}

/** 全部展开 = 空折叠记录 */
export function allExpanded(): string[] {
  return []
}

/** 打印用折叠状态: 强制全部展开 */
export function forPrint(): string[] {
  return []
}

/** 从 storage 恢复折叠记录时清洗: 只保留注册表内的字符串 key 并去重 */
export function normalizeCollapsed(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  const known = new Set(RESULT_SECTIONS.map(section => section.key))
  return [...new Set(raw.filter((item): item is string => typeof item === 'string' && known.has(item)))]
}

/** 区块锚点元素 id（scrollIntoView 目标） */
export function sectionAnchorId(key: string): string {
  return `backtest-section-${key}`
}
