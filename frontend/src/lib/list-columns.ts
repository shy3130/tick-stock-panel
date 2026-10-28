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
  // ── 日内多盘扩展列 (配置了 time_field 的时序表, 如集合竞价) ──
  /** 选盘时刻 "HH:MM[:SS]": 文字形态取该时刻或之前最近一盘的值; 缺省=末盘 */
  snapshotAt?: string
  /** 展示形态: text=文字(选定时刻值), bar=柱状迷你图, line=折线迷你图 (默认 text) */
  snapshotRender?: 'text' | 'bar' | 'line'
  /** 迷你图宽度 px (仅 bar/line), 默认 110 */
  chartWidth?: number
  /** 迷你图高度 px (仅 bar/line), 默认 36 */
  chartHeight?: number
  /** 日级粒度 (近N日) 迷你图的窗口天数, 默认 10 */
  chartDays?: number
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
  /** 日k底部量带; 默认 false。本列自管 — 与分时副图各自独立开关 */
  volume?: boolean
}

/** 日k列配置默认值（与改造前 MiniCandlestick 硬编码值一致） */
export const DEFAULT_CANDLE_CONFIG: Required<CandleColumnConfig> = {
  enabledWidth: 100,
  enabledHeight: 80,
  disabledWidth: 40,
  disabledHeight: 40,
  days: 12,
  volume: false,
}

/** 日k / 分时联动配置。唯一持久化位置：builtin:intraday 列的 intradayConfig.link。
 *  缺省(undefined)时两列完全回退原有字段 → 老配置渲染逐像素不变。
 *  link 是「配对设置」而非分时自身设置，物理寄存在分时列——不要在日k列再存
 *  第二份镜像，会分叉。
 *  只联动展开高度。量带曾做成共享开关 (link.volume 同时控两图)，实测归属不清:
 *  开关长在分时面板却同时改日k, 也无法只开一图, 已改回各列自管; 存储里残留的
 *  旧 link.volume 一律忽略 (未发布过, 无迁移负担)。 */
export interface ChartLinkConfig {
  /** 高度联动开关；严格 === true 才生效 */
  linkHeight?: boolean
  /** 联动后的展开高度 px；仅 linkHeight===true 且为有限数时生效 */
  height?: number
}

/** 联动统一高度默认值与边界（两列高度边界 32–200 的交集） */
export const DEFAULT_LINK_HEIGHT = 80
export const LINK_HEIGHT_BOUNDS = { min: 32, max: 200 } as const

/** 集合竞价叠加配置（分时列专用）。
 *
 * 竞价（9:15–9:25）与连续竞价（9:30–15:00）时间轴不重叠，在分时图左侧单独
 * 开一个窄栏：竞价价格与分时价格共用同一价格纵轴，匹配量/未匹配量以量柱
 * 画在窄栏底部。三项字段都取自同一张扩展表 —— 它们必须落在同一时间网格上，
 * 跨表拼接会产生对不齐的点列。
 */
/** 竞价窄栏宽度档: 窄/标准/宽 三档 (R3), 持久化于 IntradayAuctionConfig.width */
export type AuctionBarWidth = 'narrow' | 'standard' | 'wide'

/** 档位→竞价窄栏占整图宽度的比例; 供 MiniIntraday 与测试共用 (测试不再镜像公式) */
export const AUCTION_WIDTH_RATIOS: Record<AuctionBarWidth, number> = {
  narrow: 0.20,
  standard: 0.30,
  wide: 0.40,
}

/** 竞价窄栏默认档 (=现状 0.30, 既有视觉与测试零漂移) */
export const DEFAULT_AUCTION_WIDTH = 'standard' as const

export interface IntradayAuctionConfig {
  /** 是否显示竞价窄栏 */
  enabled?: boolean
  /** 扩展表 id (需 timeseries + 配置了时间列 time_field) */
  configId?: string
  /** 竞价价格字段 */
  priceField?: string
  /** 匹配量字段 (竞价成交量) */
  matchField?: string
  /** 未匹配量字段 (正=买方剩余, 负=卖方剩余) */
  unmatchedField?: string
  /** 竞价窄栏宽度档; 缺省/脏值 → 'standard' */
  width?: AuctionBarWidth
}

/** 分时列渲染配置（builtin: intraday 列专用） */
export interface IntradayColumnConfig {
  /** 单元格宽度 px */
  width?: number
  /** 单元格高度 px */
  height?: number
  /** 成交量副图 (R1): 底部量带 (分钟量柱; 竞价量柱不受此开关控制); 默认 false */
  volume?: boolean
  /** 左侧集合竞价叠加 */
  auction?: IntradayAuctionConfig
  /** 与日k联动 —— 唯一副本 (见 ChartLinkConfig 注释) */
  link?: ChartLinkConfig
}

/** 分时列配置默认值（宽高；竞价叠加为可选扩展） */
export const DEFAULT_INTRADAY_CONFIG = {
  width: 150,
  height: 80,
} as const

/** 分时列数值边界 */
const INTRADAY_BOUNDS = {
  width:  { min: 60, max: 300 },
  height: { min: 32, max: 200 },
} as const

export interface ResolvedIntradayConfig {
  width: number
  height: number
  /** c.volume === true 归一 (undefined/脏值 → false) */
  volume: boolean
  auction?: IntradayAuctionConfig
  /** auction.width 归一, 非法值 → 'standard' */
  auctionWidth: AuctionBarWidth
}

/** 从列数组提取联动配置的唯一读入口（渲染与配置面板共用同一份）。
 *  不筛 visible —— 隐藏分时列的 link 也要能读（写侧也不改 visible）。
 *  分时列被删除 (不再存在于 columns) 时返回 undefined，两列各自回退旧值。 */
export function resolveChartLink(columns: ColumnConfig[]): ChartLinkConfig | undefined {
  const col = columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday')
  return col?.intradayConfig?.link
}

/** 联动高度归一: linkHeight 严格 === true 且 height 为有限数才生效 (脏值回退 null,
 *  不擅自给默认 80 —— 调用方回退各列旧高度)。两个 resolve 共用同一式。 */
function resolveLinkHeight(link: ChartLinkConfig | undefined): number | null {
  return link?.linkHeight === true && Number.isFinite(link.height)
    ? clampNum(link.height, LINK_HEIGHT_BOUNDS, DEFAULT_LINK_HEIGHT)
    : null
}

/**
 * 解析分时列配置。
 *
 * link 取值: `link ?? cfg.link`。分时列是 link 的唯一宿主，因此「调用方未传」
 * 与「传入的 link 恰好为 undefined」得到同一结果 —— 都回退到 cfg.link。
 * 想按「无联动」解析时传 `{}`（空对象 = linkHeight 未开）。
 * link 只影响 height; volume 是本列自管字段, 不参与联动。
 */
export function resolveIntradayConfig(cfg: IntradayColumnConfig | undefined, link?: ChartLinkConfig): ResolvedIntradayConfig {
  const c = cfg ?? {}
  const effectiveLink = link ?? c.link
  const auctionWidth = AUCTION_WIDTH_RATIOS[c.auction?.width as AuctionBarWidth] != null
    ? c.auction!.width as AuctionBarWidth
    : DEFAULT_AUCTION_WIDTH
  const linkH = resolveLinkHeight(effectiveLink)
  return {
    width:  clampNum(c.width,  INTRADAY_BOUNDS.width,  DEFAULT_INTRADAY_CONFIG.width),
    height: linkH ?? clampNum(c.height, INTRADAY_BOUNDS.height, DEFAULT_INTRADAY_CONFIG.height),
    volume: c.volume === true,
    auction: c.auction,
    auctionWidth,
  }
}

/** 竞价叠加是否具备渲染条件（开关 + 扩展表 + 价格字段三者齐全）。 */
export function isAuctionActive(cfg: IntradayAuctionConfig | undefined): boolean {
  return !!cfg?.enabled && !!cfg.configId && !!cfg.priceField
}

/** R2 强制联动判定: 竞价开启且配了匹配量/未匹配量任一字段 → 成交量副图开关在 UI
 *  上强制置灰为开 (竞价量柱画在量带内, 不受 volume 开关控制)。只配价格字段时自由。 */
export function hasAuctionVolumeField(cfg: IntradayAuctionConfig | undefined): boolean {
  return !!cfg?.enabled && !!(cfg.matchField || cfg.unmatchedField)
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

/** resolveCandleConfig 的返回形态（与 ResolvedIntradayConfig 同构的显式接口）。 */
export interface ResolvedCandleConfig {
  enabledWidth: number
  enabledHeight: number
  disabledWidth: number
  disabledHeight: number
  days: number
  /** c.volume === true 归一 (本列自管, 与日k独立) */
  volume: boolean
}

/**
 * 合并用户配置与默认值，并对越界数值做钳制（过大取上限，过小取最小值）。
 * 返回字段齐全的配置，调用方可直接解构使用。
 * link 生效规则与 resolveIntradayConfig 同式（见 resolveLinkHeight）；
 * 只联动展开高度，宽度、收起高度与量带 (volume 本列自管) 不参与。
 */
export function resolveCandleConfig(cfg: CandleColumnConfig | undefined, link?: ChartLinkConfig): ResolvedCandleConfig {
  const c = cfg ?? {}
  const linkH = resolveLinkHeight(link)
  return {
    enabledWidth:   clampNum(c.enabledWidth,    CANDLE_BOUNDS.enabledWidth,    DEFAULT_CANDLE_CONFIG.enabledWidth),
    enabledHeight:  linkH ?? clampNum(c.enabledHeight, CANDLE_BOUNDS.enabledHeight, DEFAULT_CANDLE_CONFIG.enabledHeight),
    disabledWidth:  clampNum(c.disabledWidth,    CANDLE_BOUNDS.disabledWidth,   DEFAULT_CANDLE_CONFIG.disabledWidth),
    disabledHeight: clampNum(c.disabledHeight,   CANDLE_BOUNDS.disabledHeight,  DEFAULT_CANDLE_CONFIG.disabledHeight),
    days:           clampNum(c.days,             CANDLE_BOUNDS.days,            DEFAULT_CANDLE_CONFIG.days),
    volume:         c.volume === true,
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

/** 扩展表 schema 的最小结构 (api.extDataSchemaAll items 的结构子集, 供刷新用) */
export interface ExtTableSchemaInfo {
  id: string
  columns?: readonly { name: string; label?: string; dtype?: string; type?: string }[]
}

/** 用实时扩展表 schema 刷新 ext 列的显示名/字段类型。
 *
 *  ext 列的 label 是「添加那一刻的快照」(存于本地列配置), 在扩展数据里编辑
 *  字段标签后快照不会自己变 — 表头会一直显示旧名。这里按 configId+fieldName
 *  从实时 schema 重刷 label / source.fieldLabel / source.fieldType, 与「添加
 *  列」菜单同源; 字段已不存在于 schema 时保持原样 (字段名重命名/删除导致的
 *  死列属另一问题, 不在本函数职责内)。
 *  无任何变化时返回原数组引用 — 调用方以引用相等收敛, 不触发多余重渲染。 */
export function refreshExtColumns(
  columns: ColumnConfig[],
  extTables: readonly ExtTableSchemaInfo[] | null | undefined,
): ColumnConfig[] {
  if (!extTables || extTables.length === 0) return columns
  const tableMap = new Map(extTables.map(t => [t.id, t]))
  let changed = false
  const next = columns.map(col => {
    if (col.source?.type !== 'ext') return col
    const src = col.source as { type: 'ext'; configId: string; fieldName: string; fieldLabel?: string; fieldType?: string }
    const table = tableMap.get(src.configId)
    const field = table?.columns?.find(f => f.name === src.fieldName)
    if (!field) return col
    const label = field.label || src.fieldName
    const fieldType = field.dtype ?? field.type
    if (src.fieldLabel === label && src.fieldType === fieldType && col.label === label) return col
    changed = true
    return { ...col, label, source: { ...src, fieldLabel: label, fieldType } }
  })
  return changed ? next : columns
}

/** 按「默认方案」快照恢复列配置 (纯函数, 供 resetAllColumns 与测试共用)。
 *
 *  规则:
 *  - 快照里有的列: 恢复它的显隐与全部列级配置 (candleConfig/intradayConfig/
 *    extDisplay/standalone), 并按快照顺序排列; pinned 列始终在前且不受快照影响。
 *  - 快照里没有的列 (保存快照之后才新增的内置列/扩展字段): visible=false 且清空
 *    配置 —— 不擅自打开用户没见过的新列。
 *  - 快照为空/null: 返回 null, 调用方回退内置默认逻辑。
 */
export function restoreColumnsFromBaseline(
  current: ColumnConfig[],
  snapshot: ColumnConfig[] | null | undefined,
): ColumnConfig[] | null {
  if (!snapshot || snapshot.length === 0) return null
  const snapById = new Map(snapshot.map(c => [c.id, c]))
  const restored = current.map(c => {
    if (c.pinned) return c
    const { candleConfig, intradayConfig, extDisplay, standalone, ...rest } = c
    const saved = snapById.get(c.id)
    if (!saved) return { ...rest, visible: false }
    return {
      ...rest,
      visible: saved.visible !== false,
      ...(saved.candleConfig ? { candleConfig: saved.candleConfig } : {}),
      ...(saved.intradayConfig ? { intradayConfig: saved.intradayConfig } : {}),
      ...(saved.extDisplay ? { extDisplay: saved.extDisplay } : {}),
      ...(saved.standalone ? { standalone: saved.standalone } : {}),
    }
  })
  const order = new Map(snapshot.map((c, i) => [c.id, i]))
  const pinned = restored.filter(c => c.pinned)
  const rest = restored.filter(c => !c.pinned).sort((a, b) => {
    const ai = order.has(a.id) ? order.get(a.id)! : Number.MAX_SAFE_INTEGER
    const bi = order.has(b.id) ? order.get(b.id)! : Number.MAX_SAFE_INTEGER
    return ai - bi
  })
  return [...pinned, ...rest]
}

/** 从列配置中提取 ext 列参数，用于后端 enriched 接口。

 *  日内多盘列配置了 snapshotAt 时追加 "@HH:MM[:SS]" 后缀:
 *  后端取该时刻或之前最近一盘的值 (缺省=末盘)。 */
export function buildExtColumnsParam(columns: ColumnConfig[]): string {
  return columns
    .filter(c => c.visible && c.source.type === 'ext')
    .map(c => {
      const src = c.source as { type: 'ext'; configId: string; fieldName: string }
      const at = c.extDisplay?.snapshotAt?.trim()
      return `${src.configId}.${src.fieldName}${at ? `@${at}` : ''}`
    })
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
