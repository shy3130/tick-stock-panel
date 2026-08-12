/**
 * 通用列表列配置底座。
 *
 * 业务页面只负责定义内置列、分组和持久化 adapter；拖拽、显隐、扩展列参数等
 * 公共能力集中在这里，避免每个股票列表重复实现。
 */

export type ColumnSource =
  | { type: 'builtin'; key: string }
  | { type: 'ext'; configId: string; fieldName: string; fieldLabel?: string; fieldType?: string }
  | { type: 'computed'; key: string }

/** 扩展列字符串值渲染配置 */
export interface ExtColumnDisplayConfig {
  /** 显示模式: tag=分隔为标签, text=纯文本 */
  displayMode: 'tag' | 'text'
  /** 自定义分隔符，留空使用默认 [、,，;；-] */
  separator?: string
  /** 列最大宽度 CSS 值，如 "200px" */
  maxWidth?: string
  /** 标签显示上限，0 或 undefined=全部显示 */
  maxTags?: number
  /** 隐藏的标签索引（0-based），在 maxTags 范围内按位置隐藏 */
  hiddenIndices?: number[]
  /** 标签排列方向: horizontal=横向(默认), vertical=竖向 */
  tagLayout?: 'horizontal' | 'vertical'
  /** 数字千分位逗号(仅 number 类型有效): true=1,234,567 */
  thousandSeparator?: boolean
  /** 单位换算(仅 number 类型有效): none=不换算(默认), wan=万, yi=亿, auto=自动 */
  unitConvert?: 'none' | 'wan' | 'yi' | 'auto'
  /** 单位换算后保留小数位(仅 number 类型 + unitConvert≠none 有效), 默认 2 */
  unitDecimals?: number
}

/** 日k列渲染配置（builtin: candle 列专用） */
export interface CandleColumnConfig {
  /** 开启（显示蜡烛图）时单元格宽度 px */
  enabledWidth?: number
  /** 开启时单元格高度 px */
  enabledHeight?: number
  /** 关闭（收起）时单元格宽度 px */
  disabledWidth?: number
  /** 关闭时单元格高度 px */
  disabledHeight?: number
  /** 显示最近多少个交易日的日k */
  days?: number
}

/** 日k列配置默认值（与改造前 MiniCandlestick 硬编码值一致） */
export const DEFAULT_CANDLE_CONFIG: Required<CandleColumnConfig> = {
  enabledWidth: 100,
  enabledHeight: 80,
  disabledWidth: 40,
  disabledHeight: 40,
  days: 12,
}

/** 分时列渲染配置（builtin: intraday 列专用） */
export interface IntradayColumnConfig {
  /** 单元格宽度 px */
  width?: number
  /** 单元格高度 px */
  height?: number
}

/** 分时列配置默认值 */
export const DEFAULT_INTRADAY_CONFIG: Required<IntradayColumnConfig> = {
  width: 150,
  height: 80,
}

/** 分时列数值边界 */
const INTRADAY_BOUNDS = {
  width:  { min: 60, max: 300 },
  height: { min: 32, max: 200 },
} as const

export function resolveIntradayConfig(cfg: IntradayColumnConfig | undefined): Required<IntradayColumnConfig> {
  const c = cfg ?? {}
  return {
    width:  clampNum(c.width,  INTRADAY_BOUNDS.width,  DEFAULT_INTRADAY_CONFIG.width),
    height: clampNum(c.height, INTRADAY_BOUNDS.height, DEFAULT_INTRADAY_CONFIG.height),
  }
}

/** 数值边界（设置过大取上限，过小取最小值） */
const CANDLE_BOUNDS = {
  enabledWidth:  { min: 40,  max: 300 },
  enabledHeight: { min: 32,  max: 200 },
  disabledWidth: { min: 20,  max: 200 },
  disabledHeight:{ min: 20,  max: 200 },
  days:          { min: 1,   max: 60 },
} as const

function clampNum(v: unknown, bounds: { min: number; max: number }, fallback: number): number {
  const n = typeof v === 'number' && Number.isFinite(v) ? v : fallback
  return Math.min(bounds.max, Math.max(bounds.min, n))
}

/**
 * 合并用户配置与默认值，并对越界数值做钳制（过大取上限，过小取最小值）。
 * 返回字段齐全的配置，调用方可直接解构使用。
 */
export function resolveCandleConfig(cfg: CandleColumnConfig | undefined): Required<CandleColumnConfig> {
  const c = cfg ?? {}
  return {
    enabledWidth:   clampNum(c.enabledWidth,    CANDLE_BOUNDS.enabledWidth,    DEFAULT_CANDLE_CONFIG.enabledWidth),
    enabledHeight:  clampNum(c.enabledHeight,   CANDLE_BOUNDS.enabledHeight,   DEFAULT_CANDLE_CONFIG.enabledHeight),
    disabledWidth:  clampNum(c.disabledWidth,   CANDLE_BOUNDS.disabledWidth,   DEFAULT_CANDLE_CONFIG.disabledWidth),
    disabledHeight: clampNum(c.disabledHeight,  CANDLE_BOUNDS.disabledHeight,  DEFAULT_CANDLE_CONFIG.disabledHeight),
    days:           clampNum(c.days,            CANDLE_BOUNDS.days,            DEFAULT_CANDLE_CONFIG.days),
  }
}

export interface ColumnConfig {
  id: string        // 唯一标识，如 "builtin:price" 或 "ext:my_table:score"
  source: ColumnSource
  label: string     // 用户看到的表头名
  visible: boolean  // 是否显示
  pinned?: boolean  // 固定列不可隐藏（代码/名称、操作）
  align?: 'left' | 'center' | 'right'
  /** 扩展列显示配置（仅 ext 类型生效） */
  extDisplay?: ExtColumnDisplayConfig
  /** 日k列渲染配置（仅 builtin: candle 列生效） */
  candleConfig?: CandleColumnConfig
  /** 分时列渲染配置（仅 builtin: intraday 列生效） */
  intradayConfig?: IntradayColumnConfig
  /** 信息条场景：是否单独占一行显示（仅 StockInfoBar 生效，表格场景忽略） */
  standalone?: boolean
}

export interface ColumnGroup {
  id: string
  label: string
  icon?: string
  /** builtin/computed source key 列表 */
  keys: string[]
}

export const DEFAULT_ACTION_COLUMN_ID = 'builtin:action'

/** 序列化列配置（只保存用户可自定义的列，排除 pinned 和 action） */
export function serializeColumns(
  columns: ColumnConfig[],
  actionColumnId = DEFAULT_ACTION_COLUMN_ID,
): ColumnConfig[] {
  return columns.filter(c => !c.pinned && c.id !== actionColumnId)
}

export interface MergeColumnsOptions {
  actionColumnId?: string
  pinnedFirstIds?: string[]
}

/** 合并用户保存的列与默认列，保留用户顺序并补齐新增默认列。 */
export function mergeColumns(
  saved: ColumnConfig[] | null | undefined,
  defaults: ColumnConfig[],
  options: MergeColumnsOptions = {},
): ColumnConfig[] {
  const actionColumnId = options.actionColumnId ?? DEFAULT_ACTION_COLUMN_ID
  const pinnedFirstIds = options.pinnedFirstIds ?? ['builtin:symbol']
  const normalizedSaved = Array.isArray(saved) ? saved : []
  const result: ColumnConfig[] = []
  const savedMap = new Map(normalizedSaved.map(c => [c.id, c]))
  const defaultMap = new Map(defaults.map(c => [c.id, c]))

  // 1. 按用户保存顺序排列
  for (const col of normalizedSaved) {
    if (!col || col.id === actionColumnId) continue
    const def = defaultMap.get(col.id)
    if (def) {
      // 内置列: label/source/align/pinned 以默认定义为准；visible 使用用户配置；
      // 用户自定义的渲染配置（如日k的 candleConfig、分时的 intradayConfig、策略列的 extDisplay、信息条 standalone）需保留，否则刷新后丢失
      result.push({
        ...def,
        visible: col.visible,
        ...(col.candleConfig ? { candleConfig: col.candleConfig } : {}),
        ...(col.intradayConfig ? { intradayConfig: col.intradayConfig } : {}),
        ...(col.extDisplay ? { extDisplay: col.extDisplay } : {}),
        ...(col.standalone ? { standalone: col.standalone } : {}),
      })
    } else if (col.source?.type === 'ext') {
      // ext 列: 保留用户配置，清理旧 label 中的括号后缀
      let extCol = col
      if (col.label.includes('(') || col.label.includes('（')) {
        extCol = {
          ...col,
          label: col.source.fieldLabel || col.label.replace(/[(（].*/, '').trim() || col.source.fieldName,
        }
      }
      result.push(extCol)
    }
  }

  // 2. 补充新增的默认列
  for (const def of defaults) {
    if (!savedMap.has(def.id)) result.push(def)
  }

  // 3. 固定优先列放到最前，例如代码/名称
  for (let i = pinnedFirstIds.length - 1; i >= 0; i -= 1) {
    const id = pinnedFirstIds[i]
    const idx = result.findIndex(c => c.id === id)
    if (idx > 0) {
      const [col] = result.splice(idx, 1)
      result.unshift(col)
    }
  }

  return result
}

/** 从列配置中提取 ext 列参数，用于后端 enriched 接口。 */
export function buildExtColumnsParam(columns: ColumnConfig[]): string {
  return columns
    .filter(c => c.visible && c.source.type === 'ext')
    .map(c => `${(c.source as { type: 'ext'; configId: string; fieldName: string }).configId}.${(c.source as { type: 'ext'; configId: string; fieldName: string }).fieldName}`)
    .join(',')
}

/** 根据 ext schema 数据创建 ext 列配置。 */
export function createExtColumn(
  configId: string,
  _configLabel: string,
  fieldName: string,
  fieldLabel?: string,
): ColumnConfig {
  return {
    id: `ext:${configId}:${fieldName}`,
    source: { type: 'ext', configId, fieldName, fieldLabel },
    label: fieldLabel || fieldName,
    visible: false,
    align: 'center',
  }
}
