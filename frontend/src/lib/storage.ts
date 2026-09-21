/**
 * 集中管理所有 localStorage 持久化。
 *
 * - key 在此注册，各页面只通过 storage.xxx.get/set 调用。
 * - 类型安全，不再散落 try/catch。
 */

function kv<T>(key: string) {
  return {
    get(fallback: T): T {
      try {
        const raw = localStorage.getItem(key)
        if (raw !== null) return JSON.parse(raw) as T
      } catch { /* ignore */ }
      return fallback
    },
    set(val: T) {
      try { localStorage.setItem(key, JSON.stringify(val)) } catch { /* ignore */ }
    },
    remove() {
      try { localStorage.removeItem(key) } catch { /* ignore */ }
    },
  }
}

/** 新建策略默认基础过滤参数 (与策略 META.basic_filter 字段/单位一致: 价格元, 市值/成交额元, 换手%) */
export interface DefaultStrategyBasicFilter {
  price_min: number | null
  price_max: number | null
  float_cap_min: number | null
  float_cap_max: number | null
  amount_min: number | null
  amount_max: number | null
  turnover_min: number | null
  turnover_max: number | null
  exclude_st: boolean
  boards: string[]
}

export const storage = {
  /** 查询轮询 / SSE 配置 */
  queryConfig:          kv<unknown>('tf-stocks-query-config'),

  /** 策略池 (screener) — 统一池 (日线+分钟共用, 执行按各自声明周期路由) */
  strategyPool:         kv<string[]>('strategy-pool'),
  /** 旧分钟隔离池 — 仅作一次性迁移读取源, 迁移完成后移除该 key */
  strategyPoolMinute:   kv<string[]>('strategy-pool-1m'),

  /** 自选列表列配置 */
  watchlistColumns:     kv<unknown[]>('watchlist_columns'),

  /** 个股日K信息条指标配置 */
  stockInfoBarFields:   kv<unknown[]>('stock_info_bar_fields'),

  /** 个股日K成交量对比设置 */
  stockVolumeCompare:   kv<{ enabled: boolean; days: number }>('stock_volume_compare'),

  /** 个股详情多日分时周期 */
  stockPreviewIntradayDays: kv<number>('stock_preview_intraday_days'),

  /** 个股详情外链 URL 模板 (支持 {code}/{market}/{symbol}; 留空关闭) */
  stockExternalTemplate: kv<string>('stock_external_template'),

  /** 策略结果列表列配置 */
  screenerResultColumns: kv<unknown[]>('screener_result_columns'),

  /** 自选列表视图模式 table | card (分组卡片为临时模式, 不持久化) */
  watchlistView:        kv<string>('watchlist_view'),

  /** 自选列表日K蜡烛图显示状态 */
  watchlistCandle:      kv<boolean>('watchlist_showCandle'),

  /** 自选列表分时图显示状态 */
  watchlistIntraday:    kv<boolean>('watchlist_showIntraday'),

  /** 策略结果列表日K蜡烛图显示状态 */
  screenerCandle:       kv<boolean>('screener_showCandle'),

  /** 策略结果列表分时图显示状态 */
  screenerIntraday:     kv<boolean>('screener_showIntraday'),

  /** 策略结果列表"策略"列标签展开状态 (false=默认收起: 每行首个+计数, 行内可单独展开) */
  screenerStrategyTags: kv<boolean>('screener_strategyTagsExpanded'),

  /** 自选列表板块筛选 */
  watchlistBoardFilter: kv<string[]>('watchlist_boardFilter'),

  /** 自选列表排除 ST 标的 (默认不排除) */
  watchlistExcludeST:    kv<boolean>('watchlist_excludeST'),

  /** 自选分组统计条配置 (metric: 统计指标, sort: 排序方式, card*: 分组卡片显示项) */
  watchlistGroupStats: kv<{ metric: string; sort: string; cardTopN?: number; cardColorBar?: boolean; cardRank?: boolean }>('watchlist_groupStats'),

  /** 异动监控: 主开关 (默认关, 开启后才轮询计算; 告警走监控中心规则) */
  abnormalEnabled:      kv<boolean>('abnormal_enabled'),

  /** 异动监控: 上次计算结果 (关闭开关后仍展示, 含 asof 计算时间戳) */
  abnormalLastResult:   kv<unknown>('abnormal_last_result'),

  /** Screener 卡片尺寸 */
  screenerCardSize:     kv<string>('screener-card-size'),

  /** 连板梯队板块筛选 */
  limitLadderBoard:     kv<string[]>('limit-ladder-board-filter'),

  /** 连板梯队 ext 字段配置 */
  limitLadderExtFields: kv<Record<string, any>>('limit-ladder-ext-fields'),

  /** 连板梯队 概念/行业 显示开关 */
  limitLadderShowExt:   kv<{ concept: boolean; industry: boolean }>('limit-ladder-show-ext'),

  /** 连板梯队 涨停/跌停 切换方向 */
  limitLadderDirection: kv<'up' | 'down'>('limit-ladder-direction'),

  /** 连板梯队 封单显示模式: vol=按成交量(手), amount=按金额(元) */
  limitLadderSealMode:  kv<'vol' | 'amount'>('limit-ladder-seal-mode'),

  /** 策略创建草稿（新建专用） */
  strategyDraft: kv<{ name: string; description: string; direction: string; style?: string; rules: string; code: string; step: number; strategyId: string; source?: 'ai' | 'custom' } | null>('strategy-draft'),

  /** 新建策略默认基础过滤参数 (策略页「默认基础参数」设置; null=未自定义, 用内置默认) */
  defaultStrategyBasicFilter: kv<DefaultStrategyBasicFilter | null>('default-strategy-basic-filter'),

  /** 策略修改草稿（AI修改专用，不影响创建按钮） */
  strategyModify: kv<{ name: string; description: string; direction: string; style?: string; rules: string; code: string; step: number; strategyId: string; source?: 'ai' | 'custom' } | null>('strategy-modify'),

  /** 策略构建器草稿（旧版兼容，逐渐废弃） */
  strategyBuilderDraft: kv<{ name: string; description: string; direction: string; style?: string; rules: string; code: string; step: number; strategyId: string; source?: 'ai' | 'custom' } | null>('strategy-builder-draft'),

  /** 已保存策略的原始规则（策略ID → 规则文本） */
  strategyRules: kv<Record<string, string>>('strategy-rules'),

  /** 策略回测快捷区间按钮配置 */
  strategyBacktestQuickRanges: kv<unknown>('strategy-backtest-quick-ranges'),

  /** 策略回测最后一次成功结果和参数 */
  strategyBacktestLast: kv<{
    selectedStrategy: string | null
    symbols: string
    assetType?: 'stock' | 'etf'
    start: string
    end: string
    matching: 'close_t' | 'open_t+1'
    entryFill: 'close_t' | 'open_t+1'
    exitFill: 'close_t' | 'open_t+1' | 'signal_next_minute'
    fees: string
    stampTax?: string
    slippage: string
    maxPositions: string
    maxExposure: string
    initialCapital: string
    positionSizing: 'equal' | 'score_weight'
    mode: 'position' | 'full'
    holdingDays: string
    minuteFill?: boolean
    regimeStates?: string[]
    regimeMinScore?: number | ''
    params?: Record<string, any>
    overrides?: Record<string, any>
    strategyConfigSignature?: string
    result: any
  } | null>('strategy-backtest-last'),

  /** 概念分析页面字段配置 */
  conceptAnalysisConfig: kv<Record<string, any>>('concept-analysis-config'),

  /** 行业分析页面字段配置 */
  industryAnalysisConfig: kv<Record<string, any>>('industry-analysis-config'),

  /** 数据页画像卡片显隐 (卡片key → 是否显示) */
  dataCardVisible: kv<Record<string, boolean>>('data-card-visible'),
  /** 数据页画像卡片顺序 (卡片key 数组, 长度=卡片总数) */
  dataCardOrder: kv<string[]>('data-card-order'),
} as const
