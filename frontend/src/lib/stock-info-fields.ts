/**
 * 个股日K信息条（StockInfoBar Row 2）的指标自定义配置。
 *
 * 与自选列表列配置同源：复用 list-columns 的通用列模型与合并/序列化底座，
 * 个股详情与回测成交 K 线共用一份服务端配置。旧浏览器配置用于首次迁移。
 */

import { storage } from '@/lib/storage'
import {
  buildExtColumnsParam as buildExtColumnsParamBase,
  mergeColumns as mergeColumnsBase,
  serializeColumns as serializeInfoFields,
  type ColumnConfig,
  type ColumnGroup,
} from '@/lib/list-columns'

export type { ColumnConfig, ColumnGroup }
export { serializeInfoFields }

export interface StockInfoBarLayout {
  fields: ColumnConfig[]
  baseline: ColumnConfig[] | null
}

// ===== 内置指标注册表 =====

export const BUILTIN_INFO_FIELDS: ColumnConfig[] = [
  // 规模
  { id: 'builtin:market_cap', source: { type: 'builtin', key: 'market_cap' }, label: '市值', visible: true, align: 'left' },
  { id: 'builtin:float_market_cap', source: { type: 'builtin', key: 'float_market_cap' }, label: '流通值', visible: true, align: 'left' },
  // 成交
  { id: 'builtin:turnover', source: { type: 'builtin', key: 'turnover' }, label: '换手', visible: true, align: 'left' },
  { id: 'builtin:volume', source: { type: 'builtin', key: 'volume' }, label: '成交量', visible: false, align: 'left' },
  { id: 'builtin:amplitude', source: { type: 'builtin', key: 'amplitude' }, label: '振幅', visible: false, align: 'left' },
  // 行情
  { id: 'builtin:open', source: { type: 'builtin', key: 'open' }, label: '开盘', visible: false, align: 'left' },
  { id: 'builtin:high', source: { type: 'builtin', key: 'high' }, label: '最高', visible: false, align: 'left' },
  { id: 'builtin:low', source: { type: 'builtin', key: 'low' }, label: '最低', visible: false, align: 'left' },
  // 财务（数据来自 financials metrics 接口，默认隐藏；pe_ttm/pb 用 close 现算）
  { id: 'builtin:eps', source: { type: 'builtin', key: 'eps' }, label: 'EPS', visible: false, align: 'left' },
  { id: 'builtin:bps', source: { type: 'builtin', key: 'bps' }, label: 'BPS', visible: false, align: 'left' },
  { id: 'builtin:roe', source: { type: 'builtin', key: 'roe' }, label: 'ROE', visible: false, align: 'left' },
  { id: 'builtin:pe_ttm', source: { type: 'builtin', key: 'pe_ttm' }, label: 'PE', visible: false, align: 'left' },
  { id: 'builtin:pb', source: { type: 'builtin', key: 'pb' }, label: 'PB', visible: false, align: 'left' },
  { id: 'builtin:gross_margin', source: { type: 'builtin', key: 'gross_margin' }, label: '毛利率', visible: false, align: 'left' },
  { id: 'builtin:net_margin', source: { type: 'builtin', key: 'net_margin' }, label: '净利率', visible: false, align: 'left' },
  { id: 'builtin:debt_ratio', source: { type: 'builtin', key: 'debt_ratio' }, label: '负债率', visible: false, align: 'left' },
  { id: 'builtin:revenue_yoy', source: { type: 'builtin', key: 'revenue_yoy' }, label: '营收增速', visible: false, align: 'left' },
  { id: 'builtin:net_income_yoy', source: { type: 'builtin', key: 'net_income_yoy' }, label: '净利增速', visible: false, align: 'left' },
]

export const INFO_GROUPS: ColumnGroup[] = [
  { id: 'scale', label: '规模', icon: '🏦', keys: ['market_cap', 'float_market_cap'] },
  { id: 'volume', label: '成交', icon: '📊', keys: ['turnover', 'volume', 'amplitude'] },
  { id: 'quote', label: '行情', icon: '📈', keys: ['open', 'high', 'low'] },
  { id: 'finance', label: '财务', icon: '📋', keys: ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'debt_ratio', 'revenue_yoy', 'net_income_yoy'] },
]

/** 读取旧浏览器存量，保留顺序、隐藏字段、显示格式和默认方案。 */
export function loadLegacyInfoBarLayout(): StockInfoBarLayout | null {
  const storedFields = storage.stockInfoBarFields.get([])
  const storedBaseline = storage.stockInfoBarFieldsBaseline.get(null)
  const fields = Array.isArray(storedFields) ? storedFields.filter(isInfoField) : []
  const baseline = Array.isArray(storedBaseline) ? storedBaseline.filter(isInfoField) : null
  return fields.length || baseline?.length ? { fields, baseline } : null
}

function isInfoField(value: unknown): value is ColumnConfig {
  if (typeof value !== 'object' || value === null) return false
  const field = value as Partial<ColumnConfig>
  return typeof field.id === 'string' && typeof field.label === 'string' && typeof field.visible === 'boolean'
    && !!field.source && (field.source.type === 'ext'
      ? typeof field.source.configId === 'string' && typeof field.source.fieldName === 'string'
      : typeof field.source.key === 'string')
}

/** 从信息条字段配置中提取 ext 列参数（逗号分隔 config_id.field_name），用于 klineDaily 接口。 */
export function buildInfoExtColumnsParam(columns: ColumnConfig[]): string {
  return buildExtColumnsParamBase(columns)
}

/** 保留保存的顺序和显隐，补齐当前版本新增的内置字段。 */
export function mergeInfoFields(fields?: ColumnConfig[]): ColumnConfig[] {
  return mergeColumnsBase(fields, BUILTIN_INFO_FIELDS, { pinnedFirstIds: [] })
}
