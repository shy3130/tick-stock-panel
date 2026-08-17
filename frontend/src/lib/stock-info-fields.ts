/**
 * 个股日K信息条（StockInfoBar Row 2）的指标自定义配置。
 *
 * 与自选列表列配置同源：复用 list-columns 的通用列模型与合并/序列化底座，
 * 仅做纯 localStorage 同步持久化（无后端双写）。个股预览弹窗与回测成交K线
 * Modal 共用同一份配置。
 */

import { storage } from '@/lib/storage'
import {
  buildExtColumnsParam as buildExtColumnsParamBase,
  mergeColumns as mergeColumnsBase,
  serializeColumns as serializeColumnsBase,
  type ColumnConfig,
  type ColumnGroup,
} from '@/lib/list-columns'

export type { ColumnConfig, ColumnGroup }

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
  // 注解
  { id: 'builtin:tags', source: { type: 'builtin', key: 'tags' }, label: '标签', visible: false, align: 'left', extDisplay: { displayMode: 'tag', maxTags: 5 } },
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
  { id: 'note', label: '注解', icon: '🏷️', keys: ['tags'] },
  { id: 'finance', label: '财务', icon: '📋', keys: ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'debt_ratio', 'revenue_yoy', 'net_income_yoy'] },
]

// ===== localStorage 持久化 =====

/** 加载信息条指标配置：localStorage → 默认值，自动补齐新增默认项。 */
export function loadInfoFields(): ColumnConfig[] {
  const saved = storage.stockInfoBarFields.get([]) as ColumnConfig[]
  if (saved.length === 0) return [...BUILTIN_INFO_FIELDS]
  return mergeFields(saved, BUILTIN_INFO_FIELDS)
}

/** 保存信息条指标配置到 localStorage。 */
export function saveInfoFields(columns: ColumnConfig[]): void {
  storage.stockInfoBarFields.set(serializeFields(columns))
}

/** 序列化（此处无 pinned/action 列，直接用底座默认实现）。 */
function serializeFields(columns: ColumnConfig[]): ColumnConfig[] {
  return serializeColumnsBase(columns)
}

/** 从信息条字段配置中提取 ext 列参数（逗号分隔 config_id.field_name），用于 klineDaily 接口。 */
export function buildInfoExtColumnsParam(columns: ColumnConfig[]): string {
  return buildExtColumnsParamBase(columns)
}

/** 合并用户保存的配置与默认配置。 */
function mergeFields(saved: ColumnConfig[], defaults: ColumnConfig[]): ColumnConfig[] {
  // 无固定列，传入空的 pinnedFirstIds 跳过「代码置顶」逻辑
  return mergeColumnsBase(saved, defaults, { pinnedFirstIds: [] })
}
