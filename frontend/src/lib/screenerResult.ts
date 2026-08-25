import type {
  ScreenerCondition,
  ScreenerConditionGroup,
  ScreenerFacetKey,
  ScreenerFieldSpec,
  ScreenerGroupLogic,
  ScreenerIndustryFacetItem,
  ScreenerOrderBy,
  ScreenerQueryFacets,
  ScreenerQueryRequest,
} from './api'
import { SCREENER_CONDITION_GROUPS } from './api'

/** 结果列定义（导出 CSV 与表格渲染共用） */
export interface ResultColumn {
  field: string
  label: string
}

/** 单元格按 RFC 4180 转义：含逗号/引号/换行时用双引号包裹并双写引号 */
export function csvEscapeCell(value: string): string {
  if (/[",\r\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`
  return value
}

/**
 * 把结果行导出为 CSV 文本（不含 BOM；下载时由 downloadResultCsv 补 BOM，
 * 避免 Excel 打开中文乱码）。列顺序与传入 columns 一致。
 */
export function toResultCsv(columns: ResultColumn[], rows: Record<string, unknown>[]): string {
  const header = columns.map(column => csvEscapeCell(column.label)).join(',')
  const body = rows.map(row =>
    columns
      .map(column => {
        const value = row[column.field]
        return csvEscapeCell(value == null ? '' : String(value))
      })
      .join(','),
  )
  return [header, ...body].join('\r\n')
}

/** 触发浏览器下载 CSV（UTF-8 + BOM，Excel 可直接打开） */
export function downloadResultCsv(filename: string, csv: string): void {
  const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

/** F8: 仅最新日字段的徽标文案；非 latest_only 字段返回 null（不渲染徽标） */
export function latestOnlyBadgeLabel(spec: Pick<ScreenerFieldSpec, 'availability'>): string | null {
  return spec.availability === 'latest_only' ? '仅最新日' : null
}

export function isLatestOnlyField(
  field: string | undefined,
  fieldsByName: Map<string, ScreenerFieldSpec>,
): boolean {
  if (!field) return false
  return fieldsByName.get(field)?.availability === 'latest_only'
}

/**
 * F8: 按截止日期拆分条件。
 * historical=true（asOf 已设且 ≠ 最新日）时，仅最新日字段的条件被剔除，
 * 保证历史日期下无需这些字段也能查询；返回被剔除的条数供 UI 提示。
 * sequence 字段 availability≠latest_only，历史日下保留（不进剔除集）。
 */
export function splitConditionsForAsOf(
  conditions: ScreenerCondition[],
  fieldsByName: Map<string, ScreenerFieldSpec>,
  historical: boolean,
): { applicable: ScreenerCondition[]; droppedCount: number } {
  if (!historical) return { applicable: conditions, droppedCount: 0 }
  const applicable = conditions.filter(condition => !isLatestOnlyField(condition.field, fieldsByName))
  return { applicable, droppedCount: conditions.length - applicable.length }
}

const DEFAULT_CONDITION_GROUP: ScreenerConditionGroup = 'A'

/** F14: 旧/未分组条件映射到 A；非法值回落 A */
export function normalizeConditionGroup(group: string | null | undefined): ScreenerConditionGroup {
  const raw = typeof group === 'string' ? group.trim().toUpperCase() : ''
  return (SCREENER_CONDITION_GROUPS as readonly string[]).includes(raw)
    ? (raw as ScreenerConditionGroup)
    : DEFAULT_CONDITION_GROUP
}

/** F14: 序列化条件时始终带规范化 group（A-E） */
export function serializeScreenerConditions(conditions: ScreenerCondition[]): ScreenerCondition[] {
  return conditions.map(condition => ({
    field: condition.field,
    op: condition.op,
    value: condition.value,
    group: normalizeConditionGroup(condition.group),
  }))
}

/** F14: 条件中出现的不同分组集合（已规范化） */
export function uniqueConditionGroups(conditions: ScreenerCondition[]): ScreenerConditionGroup[] {
  const seen = new Set<ScreenerConditionGroup>()
  for (const condition of conditions) {
    seen.add(normalizeConditionGroup(condition.group))
  }
  return SCREENER_CONDITION_GROUPS.filter(group => seen.has(group))
}

/**
 * F14: 仅当 ≥2 个不同组时组间 OR 才有语义；否则强制 and（与旧 flat AND 兼容）。
 */
export function effectiveGroupLogic(
  conditions: ScreenerCondition[],
  groupLogic: ScreenerGroupLogic | null | undefined,
): ScreenerGroupLogic {
  if (uniqueConditionGroups(conditions).length < 2) return 'and'
  return groupLogic === 'or' ? 'or' : 'and'
}

export interface BuildScreenerQueryInput {
  conditions: ScreenerCondition[]
  limit: number
  asOf?: string
  orderBy?: ScreenerOrderBy
  groupLogic?: ScreenerGroupLogic | null
  facets?: ScreenerFacetKey[]
}

/** F14+F15: 条件查询请求体（group / group_logic / facets） */
export function buildScreenerQueryRequest(input: BuildScreenerQueryInput): ScreenerQueryRequest {
  const conditions = serializeScreenerConditions(input.conditions)
  const limit = Math.min(500, Math.max(1, input.limit))
  const facets = input.facets ?? (['industry'] satisfies ScreenerFacetKey[])
  return {
    conditions,
    limit,
    group_logic: effectiveGroupLogic(conditions, input.groupLogic),
    facets,
    ...(input.asOf ? { as_of: input.asOf } : {}),
    ...(input.orderBy ? { order_by: input.orderBy } : {}),
  }
}

export interface IndustryFacetDisplay {
  items: Array<[string, number]>
  missing: number
  total: number
  max: number
}

/**
 * F15: 将后端 facets.industry 转为展示数据。
 * 空/缺失 → null（整卡隐藏）；total 用 count 之和（全量命中，非 rows.length）。
 */
export function industryFacetDisplay(
  facets: ScreenerQueryFacets | null | undefined,
  resultTotal?: number | null,
): IndustryFacetDisplay | null {
  const raw = facets?.industry
  if (!Array.isArray(raw) || raw.length === 0) return null

  const items: Array<[string, number]> = []
  let covered = 0
  for (const item of raw as ScreenerIndustryFacetItem[]) {
    const value = typeof item?.value === 'string' ? item.value.trim() : ''
    const count = typeof item?.count === 'number' && Number.isFinite(item.count) ? item.count : 0
    if (!value || count <= 0) continue
    items.push([value, count])
    covered += count
  }
  if (items.length === 0) return null

  items.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'))
  const total = typeof resultTotal === 'number' && Number.isFinite(resultTotal) && resultTotal > 0
    ? resultTotal
    : covered
  const missing = Math.max(0, total - covered)
  return { items, missing, total, max: items[0][1] }
}

/** F15: facet_warnings 轻量文案；未知码原样回退 */
export function facetWarningText(code: string): string {
  if (code === 'industry_unavailable') return '行业分布暂不可用（财务快照缺失）'
  return code
}
