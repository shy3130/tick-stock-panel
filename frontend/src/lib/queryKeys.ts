/**
 * 集中管理所有 React Query key。
 *
 * - 新增查询只需在此加一行，所有消费方自动引用。
 * - SSE invalidation 基于 SSE_INVALIDATE_PREFIXES 列表，新增 key 无需改 useQuoteStream。
 */

// ===== Query Key 工厂 =====

export const QK = {
  // 全局 / 共享 (Layout 预取)
  capabilities:   ['capabilities'] as const,
  settings:       ['settings'] as const,
  endpoints:      ['endpoints'] as const,
  version:        ['version'] as const,
  preferences:    ['preferences'] as const,
  quoteStatus:    ['quote-status'] as const,
  quoteInterval:  ['quote-interval'] as const,
  overviewMarket: (asOf?: string) => ['overview-market', asOf ?? 'latest'] as const,
  indexQuotes:    ['index-quotes'] as const,
  indexList:      ['index-list'] as const,

  // Watchlist
  watchlist:            ['watchlist'] as const,
  watchlistQuotes:      ['watchlist-quotes'] as const,
  watchlistEnriched:    (ext?: string) => ['watchlist-enriched', ext] as const,
  watchlistKlineBatch:  (symbols: string) => ['watchlist-kline-batch', symbols] as const,
  // 前缀 watchlist- 以便 SSE quotes_updated 经 SSE_INVALIDATE_PREFIXES 命中
  watchlistSnapshot:    (symbols: string) => ['watchlist-snapshot', symbols] as const,
  watchlistGroups:     ['watchlist-groups'] as const,

  instrumentSearch: (q: string, assetTypes?: readonly string[], limit = 20) => {
    const normalizedAssetTypes = assetTypes?.length
      ? [...new Set(assetTypes)].sort().join(',')
      : 'all'
    return ['instrument-search', q, normalizedAssetTypes, limit] as const
  },

  // Screener
  screener:             ['screener'] as const,
  screenerStrategies:   ['screener-strategies'] as const,
  screenerCached:       (ext?: string) => ['screener-cached', ext] as const,
  screenerKlineBatch:   (symbols: string) => ['screener-kline-batch', symbols] as const,
  screenerScreens:     ['screener-screens'] as const,
  marketSnapshot:       ['market-snapshot'] as const,
  limitLadder:          (asOf?: string) => ['limit-ladder', asOf] as const,

  // Backtest
  backtestStatus:       ['backtest-status'] as const,

  // Data / Pipeline
  dataStatus:           ['data-status'] as const,
  canonicalHistoryStatus: ['canonical-history-status'] as const,
  pipelineJobs:         ['pipeline-jobs'] as const,
  pipelineJob:          (id: string) => ['pipeline-job', id] as const,
  extData:              ['ext-data'] as const,
  extDataRows:          (id: string, date?: string, limit?: number, columns?: string) => ['ext-data-rows', id, date, limit, columns] as const,
  analysisMenus:        ['analysis-menus'] as const,
  analysisMenu:         (id: string) => ['analysis-menu', id] as const,

  // Kline
  kline:                (symbol: string, start: string, end: string, extColumns?: string) =>
                           ['kline', symbol, start, end, extColumns ?? ''] as const,
  stockLevels:          (symbol: string, days?: number) => ['stock-levels', symbol, days ?? 120] as const,
  klineMinute:          (symbol: string, date: string) =>
                             ['kline-minute', symbol, date] as const,
  indexDaily:           (symbol: string, start: string, end: string) =>
                           ['index-daily', symbol, start, end] as const,
  indexMinute:          (symbol: string, date: string) =>
                           ['index-minute', symbol, date] as const,

  // Schema
  extDataSchemaAll:     ['ext-data-schema-all'] as const,
  tableSchema:          (table: string) => ['table-schema', table] as const,

  // Custom Signals
  customSignals:        ['custom-signals'] as const,
  customSignalsOptions: ['custom-signals-options'] as const,

  // Monitor (监控规则 + 触发记录)
  monitorRules:         ['monitor-rules'] as const,
  monitorRuleOptions:   ['monitor-rule-options'] as const,
  abnormalOverview:    (filter?: string) => ['abnormal-overview', filter ?? ''] as const,
  alerts:               (source?: string) => ['alerts', source ?? ''] as const,


  // AI 大盘复盘
  reviewReports:        ['review-reports'] as const,

  // 复盘数据分区(情绪周期 / 连板天梯 / 题材轮动 / 风险线索)
  reviewEmotion:        (asOf: string | undefined, days: number) => ['review-emotion', asOf ?? 'latest', days] as const,
  reviewLadder:         (asOf: string | undefined, days: number) => ['review-ladder', asOf ?? 'latest', days] as const,
  reviewRotation:       (asOf: string | undefined, days: number, top: number) => ['review-rotation', asOf ?? 'latest', days, top] as const,
  reviewClues:          (asOf: string | undefined, limit: number) => ['review-clues', asOf ?? 'latest', limit] as const,

  // 港股复盘分区(市场宽度 / 涨跌榜)
  reviewHkBreadth:      (asOf: string | undefined, days: number) => ['review-hk-breadth', asOf ?? 'latest', days] as const,
  reviewHkMovers:       (asOf: string | undefined, limit: number) => ['review-hk-movers', asOf ?? 'latest', limit] as const,

  // 概念涨幅轮动矩阵
  rpsRotation:          (days: number) => ['rps-rotation', days] as const,

  // Market Data（只读上游发布快照；用户触发查询）
  marketDataStatus:       ['market-data-status'] as const,
  marketDataChip:         (symbol: string, start: string, end: string, limit: number) =>
                            ['market-data-chip', symbol, start, end, limit] as const,
  marketDataMoneyflowStock: (symbol: string, freq: 'daily' | 'minute', start: string, end: string) =>
                            ['market-data-moneyflow-stock', symbol, freq, start, end] as const,
  marketDataMoneyflowBlocks: (freq: 'daily' | 'minute', date: string, blockType: number | undefined, limit: number) =>
                            ['market-data-moneyflow-blocks', freq, date, blockType ?? 'all', limit] as const,
  marketDataCallAuction:  (symbol: string, date: string, session: string | undefined, limit: number) =>
                            ['market-data-call-auction', symbol, date, session ?? 'all', limit] as const,
  marketDataTransactions: (symbol: string, date: string, limit: number) =>
                            ['market-data-transactions', symbol, date, limit] as const,


  // Trading (结构化计划检查 · M25 连续性链)
  planCheckContinuity: (attemptId: string) => ['plan-check-continuity', attemptId] as const,
} as const

// ===== SSE 应该 invalidate 的 key 前缀列表 =====
// 新增需要 SSE 推送的查询，只需在此加一行

export const SSE_INVALIDATE_PREFIXES = [
  'watchlist',
  'quote-status',
  'index-quotes',
  'overview-market',
  'limit-ladder',
  'screener',
] as const
