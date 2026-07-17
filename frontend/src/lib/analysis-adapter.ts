/**
 * 概念/行业分析 — 数据适配层
 *
 * 处理两种扩展数据结构：
 * - 结构 A（个股维度）：每行一只股票，维度字段（如 concept）存该股票所属的概念/行业
 * - 结构 B（板块维度）：每行一个概念/行业，带成分股列表（如 constituents: [...]）
 *
 * 两种结构统一输出为 DimensionGroup[]，供页面组件消费。
 */

import type { ExtDataConfig, ExtDataField, ExtDataRowsResult } from '@/lib/api'

// ===== 公共类型 =====

export interface StockRow {
  symbol: string
  code?: string
  name?: string
  [key: string]: unknown
}

export interface DimensionGroup {
  /** 维度名称（概念名/行业名） */
  key: string
  /** 成分股数量 */
  count: number
  /** 成分股原始行 */
  stocks: StockRow[]
  /** 聚合指标（如涨跌幅均值等） */
  metrics: Record<string, number | null>
}

export interface ResolvedDimension {
  /** 是否成功解析 */
  ok: boolean
  /** 数据结构类型 */
  structure: 'per_stock' | 'per_dimension' | 'unknown'
  /** 维度字段名 */
  dimensionField: string
  /** 所有解析出的分组 */
  groups: DimensionGroup[]
  /** 原始全部行（结构 A 下为原始行，结构 B 下展平后的全部成分股） */
  allStocks: StockRow[]
  /** 解析提示 */
  hint?: string
}

// ===== 结构探测 =====

const SEPARATORS = /[、,，;；|/\s]+/
const CONSTITUENT_KEYS = [
  'constituents', '成分股', 'stocks', 'members', 'codes', 'list',
  'symbol_list', 'stock_list', 'member_list',
]
const DIMENSION_NAME_KEYS = [
  'name', '概念名称', '概念', '行业名称', '行业', '板块名称', '板块',
  'concept', 'industry', 'sector', 'theme', 'title', 'label',
]
const INVALID_DIMENSION_VALUES = new Set(['nan', 'none', 'null'])

function dimensionValue(raw: unknown): string {
  const text = String(raw ?? '').trim()
  return INVALID_DIMENSION_VALUES.has(text.toLowerCase()) ? '' : text
}

function dimensionValues(raw: unknown): string[] {
  if (raw == null) return []
  return String(raw).split(SEPARATORS)
    .map(dimensionValue)
    .filter(Boolean)
}

/** 检测行是否是"板块维度"结构（含成分股列表字段） */
function detectConstituentField(fields: ExtDataField[]): string | null {
  return fields.find(f =>
    CONSTITUENT_KEYS.some(k => f.name.toLowerCase() === k.toLowerCase() || f.label?.includes(k))
  )?.name ?? null
}

/** 检测维度名称字段 */
function detectDimensionNameField(fields: ExtDataField[]): string | null {
  return fields.find(f =>
    DIMENSION_NAME_KEYS.some(k => f.name.toLowerCase() === k.toLowerCase() || f.label?.includes(k))
  )?.name ?? null
}

/** 从候选名中选取最佳维度字段（结构 A） */
export function pickDimensionField(
  fields: ExtDataField[],
  candidates: string[],
): string {
  const nonMeta = fields.filter(f =>
    !['symbol', 'code', 'name', '股票简称', '股票代码', 'date'].includes(f.name)
  )
  for (const c of candidates) {
    const m = nonMeta.find(f =>
      f.name.toLowerCase().includes(c.toLowerCase()) ||
      f.label?.toLowerCase().includes(c.toLowerCase())
    )
    if (m) return m.name
  }
  // 回退：第一个非数值字段
  return nonMeta.find(f => f.dtype !== 'int' && f.dtype !== 'float')?.name ?? nonMeta[0]?.name ?? ''
}

/** 判断字段是否为数值类型 */
function isNumericField(f: ExtDataField): boolean {
  return f.dtype === 'int' || f.dtype === 'float'
}

// ===== 结构 A 解析：个股维度 =====

function parsePerStock(
  rows: Record<string, any>[],
  dimensionField: string,
  numericFields: string[],
): DimensionGroup[] {
  const map = new Map<string, StockRow[]>()

  for (const row of rows) {
    // 支持多值分隔（如 "人工智能,芯片,5G"）
    const values = dimensionValues(row[dimensionField])
    if (!values.length) continue
    const stock: StockRow = { ...row, symbol: row.symbol ?? row.code ?? '' }

    for (const v of values) {
      const list = map.get(v) ?? []
      list.push(stock)
      map.set(v, list)
    }
  }

  return [...map.entries()]
    .map(([key, stocks]) => ({
      key,
      count: stocks.length,
      stocks,
      metrics: computeMetrics(stocks, numericFields),
    }))
    .sort((a, b) => b.count - a.count)
}

// ===== 结构 B 解析：板块维度 =====

function parsePerDimension(
  rows: Record<string, any>[],
  constituentField: string,
  nameField: string,
  numericFields: string[],
): DimensionGroup[] {
  const allStocks: StockRow[] = []

  const groups = rows.map(row => {
    const key = dimensionValue(row[nameField] ?? row[constituentField])
    if (!key) return null

    // 成分股可能是字符串数组、对象数组、逗号分隔字符串
    const rawList = row[constituentField]
    const stocks = parseConstituents(rawList)

    stocks.forEach(s => { if (s.symbol) allStocks.push(s) })

    // 维度自身的数值指标也保留
    const metrics = computeMetrics(stocks, numericFields)
    // 补上行级别的数值
    for (const f of numericFields) {
      if (typeof row[f] === 'number') {
        metrics[`__dim_${f}`] = row[f]
      }
    }

    return { key, count: stocks.length, stocks, metrics } as DimensionGroup
  }).filter((g): g is DimensionGroup => g !== null && g.key !== '')

  return groups.sort((a, b) => b.count - a.count)
}

/** 解析成分股字段（支持多种格式） */
function parseConstituents(raw: unknown): StockRow[] {
  if (raw == null) return []
  if (typeof raw === 'string') {
    // 逗号/分隔符分隔的股票代码字符串
    return raw.split(SEPARATORS).map(s => s.trim()).filter(Boolean).map(s => ({
      symbol: normalizeSymbol(s),
      code: s,
    }))
  }
  if (Array.isArray(raw)) {
    return raw.map(item => {
      if (typeof item === 'string') {
        return { symbol: normalizeSymbol(item), code: item }
      }
      if (typeof item === 'object' && item !== null) {
        const obj = item as Record<string, any>
        return {
          symbol: obj.symbol ?? obj.code ?? obj.股票代码 ?? '',
          code: obj.code ?? obj.symbol ?? '',
          name: obj.name ?? obj.股票简称 ?? obj.名称 ?? '',
          ...obj,
        }
      }
      return { symbol: String(item) }
    })
  }
  return []
}

function normalizeSymbol(s: string): string {
  // 尝试补全为 6 位代码
  if (/^\d{6}$/.test(s)) return s
  return s
}

// ===== 聚合指标计算 =====

function computeMetrics(
  stocks: StockRow[],
  numericFields: string[],
): Record<string, number | null> {
  const result: Record<string, number | null> = {}
  for (const f of numericFields) {
    const vals = stocks
      .map(s => s[f])
      .filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
    if (vals.length === 0) {
      result[f] = null
    } else {
      result[f] = vals.reduce((a, b) => a + b, 0) / vals.length
    }
  }
  return result
}

// ===== 主入口：自动探测 + 解析 =====

export function resolveDimension(
  data: ExtDataRowsResult | null | undefined,
  config: ExtDataConfig | null | undefined,
  candidateFields: string[],
): ResolvedDimension {
  if (!data || !config || !data.rows.length) {
    return { ok: false, structure: 'unknown', dimensionField: '', groups: [], allStocks: [] }
  }

  const fields = data.fields ?? config.fields
  const rows = data.rows
  const numericFields = fields.filter(f => isNumericField(f)).map(f => f.name)

  // 先检测是否为结构 B（板块维度）
  const constituentField = detectConstituentField(fields)
  if (constituentField) {
    const nameField = detectDimensionNameField(fields) ?? 'name'
    const groups = parsePerDimension(rows, constituentField, nameField, numericFields)
    const allStocks = groups.flatMap(g => g.stocks)
    return {
      ok: true,
      structure: 'per_dimension',
      dimensionField: nameField,
      groups,
      allStocks,
      hint: `检测到板块维度结构（成分股字段: ${constituentField}）`,
    }
  }

  // 结构 A（个股维度）
  const dimensionField = pickDimensionField(fields, candidateFields)
  if (!dimensionField) {
    return {
      ok: false,
      structure: 'unknown',
      dimensionField: '',
      groups: [],
      allStocks: rows as StockRow[],
      hint: '未找到合适的维度字段',
    }
  }

  const groups = parsePerStock(rows, dimensionField, numericFields)
  const allStocks = rows as StockRow[]

  return {
    ok: true,
    structure: 'per_stock',
    dimensionField,
    groups,
    allStocks,
    hint: `按 ${dimensionField} 分组，共 ${groups.length} 个维度`,
  }
}

// ===== 行情数据关联 =====

export interface QuoteMap {
  symbol: string
  price?: number
  pct?: number
  change_pct?: number
  name?: string
  [key: string]: unknown
}

/** 构建 symbol → quote 的快速查找 */
export function buildQuoteMap(quotes: QuoteMap[]): Map<string, QuoteMap> {
  const map = new Map<string, QuoteMap>()
  for (const q of quotes) {
    if (q.symbol) map.set(q.symbol, q)
    // 也用纯数字代码做索引
    const code = q.symbol?.replace(/\.\w+$/, '')
    if (code) map.set(code, q)
  }
  return map
}

/** 为分组计算行情聚合指标 */
export function computeQuoteMetrics(
  stocks: StockRow[],
  quoteMap: Map<string, QuoteMap>,
): {
  avgPct: number | null
  upCount: number
  downCount: number
  flatCount: number
  totalVolume: number
} {
  let up = 0, down = 0, flat = 0, totalVol = 0
  let sumPct = 0, countPct = 0

  for (const s of stocks) {
    const sym = String(s.symbol ?? '')
    const q = quoteMap.get(sym) ?? quoteMap.get(sym.replace(/\.\w+$/, ''))
    if (!q) continue
    const pct = q.pct ?? q.change_pct
    if (pct != null && typeof pct === 'number' && Number.isFinite(pct)) {
      sumPct += pct
      countPct++
      if (pct > 0) up++
      else if (pct < 0) down++
      else flat++
    }
    totalVol += (typeof q.price === 'number' ? 1 : 0) // 简化计数
  }

  return {
    avgPct: countPct > 0 ? sumPct / countPct : null,
    upCount: up,
    downCount: down,
    flatCount: flat,
    totalVolume: totalVol,
  }
}
