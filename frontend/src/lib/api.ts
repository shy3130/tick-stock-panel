// 后端 API 客户端 — 全项目统一入口
//
// Dev:Vite 代理 /api 到 :3018
// Prod:同源(FastAPI 托管前端 dist)

import { toast } from '@/components/Toast'

const BASE = ''

async function request<T>(path: string, init?: RequestInit, opts?: { silent404?: boolean }): Promise<T> {
  const isFormData = init?.body instanceof FormData
  const headers: Record<string, string> = {}
  if (!isFormData) headers['Content-Type'] = 'application/json'
  const res = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!res.ok) {
    // 404 对调用方是"无数据"语义(如尚未生成 AI 归因)时静默返回 null,不弹 toast
    if (opts?.silent404 && res.status === 404) return null as T
    let detail = ''
    try {
      const j = JSON.parse(await res.text())
      const rawDetail = j.detail ?? j.message ?? ''
      detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail)
    } catch { /* ignore */ }
    const msg = detail || `${res.status} ${res.statusText}`
    // 401 (未登录/会话过期) 不弹 toast — 由全局认证拦截器统一跳登录页, 避免刷屏
    if (res.status !== 401) toast(msg, 'error')
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// ===== Capabilities =====
export interface CapabilityLimits {
  rpm: number | null
  batch: number | null
  subscribe: number | null
}

export interface CapabilitiesResponse {
  label: string
  capabilities: Record<string, CapabilityLimits>
}

export interface InstrumentSearchResult {
  symbol: string
  name: string
  code: string
  asset_type?: 'stock' | 'index' | 'etf' | 'hk' | 'unknown' | string
  source?: 'local' | 'eastmoney_suggest' | string
  matched_by?: 'code' | 'symbol' | 'name' | 'pinyin' | 'initials' | 'suggest' | string
}

export interface AgentMsg {
  role: 'user' | 'assistant'
  content: string
  display_content?: string
}

export interface AgentToolTrace {
  name: string
  args?: Record<string, unknown>
  result?: unknown
  elapsed_ms?: number
}

export interface AgentTool {
  name: string
  description: string
  read_only?: boolean
}

export type AgentEvent =
  | { type: 'attempt_start'; attempt_id: string; session_id?: string }
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; name: string; result: unknown; elapsed_ms?: number }
  | { type: 'delta'; content: string }
  | { type: 'cancelled'; attempt_id: string }
  | { type: 'done'; elapsed_ms?: number }
  | { type: 'error'; message: string; elapsed_ms?: number }

export interface AgentSession {
  session_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
  last_attempt_id?: string | null
  last_attempt_status?: 'running' | 'done' | 'cancelled' | 'error' | null
}

export interface AgentStoredMessage extends AgentMsg {
  message_id: string
  created_at: string
  tool_traces?: AgentToolTrace[]
  elapsed_ms?: number
}

export interface DocumentEnvelope {
  source: string
  kind: string
  title: string
  text: string
  char_count: number
  truncated: boolean
  warnings: string[]
}

// ===== Financials =====
export interface FinancialStatus {
  available: boolean
  tables: Record<string, { rows: number; symbols: number }>
  last_sync: Record<string, string>
  /** 服务端是否正在同步(手动触发)——驱动"同步中"UI 并防重复点击 */
  syncing?: boolean
}

export interface FinancialMetricRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  eps_basic?: number | null
  eps_diluted?: number | null
  bps?: number | null
  ocfps?: number | null
  roe?: number | null
  roe_diluted?: number | null
  roa?: number | null
  gross_margin?: number | null
  net_margin?: number | null
  debt_to_asset_ratio?: number | null
  revenue_yoy?: number | null
  net_income_yoy?: number | null
  operating_cash_to_revenue?: number | null
  inventory_turnover?: number | null
  [key: string]: any
}

export interface FinancialIncomeRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  revenue?: number | null
  operating_cost?: number | null
  operating_profit?: number | null
  total_profit?: number | null
  net_income?: number | null
  net_income_attributable?: number | null
  basic_eps?: number | null
  diluted_eps?: number | null
  [key: string]: any
}

export interface FinancialBalanceSheetRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  total_assets?: number | null
  total_current_assets?: number | null
  cash_and_equivalents?: number | null
  total_liabilities?: number | null
  total_equity?: number | null
  equity_attributable?: number | null
  [key: string]: any
}

export interface FinancialCashFlowRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  net_operating_cash_flow?: number | null
  net_investing_cash_flow?: number | null
  net_financing_cash_flow?: number | null
  capex?: number | null
  net_cash_change?: number | null
  [key: string]: any
}

/** AI 财务分析历史报告 */
export interface AiFinancialReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  periods?: number
  summary?: string
  created_at: string
}

// ===== 个股分析 =====
export type LevelType = 'sr' | 'pivot' | 'extreme' | 'boll' | 'keltner_s' | 'keltner_m' | 'keltner_l' | 'atr_stop' | 'gap' | 'fib' | 'round'

export interface PriceLevel {
  value: number
  label: string
  type: LevelType
  side: 'resistance' | 'support' | 'neutral'
  strength?: 'strong' | 'medium' | 'weak'
  /** 档位(仅 pivot 有):0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3。前端按"显示到第几档"过滤。 */
  rank?: number
}

/** 带状曲线指标(布林带/Keltner/ATR)的每日时间序列,与 dates 对齐。 */
export interface LevelSeries {
  boll?: { upper: (number | null)[]; lower: (number | null)[]; mid?: (number | null)[] }
  keltner_s?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_m?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_l?: { upper: (number | null)[]; lower: (number | null)[] }
  atr?: { stop_loss: (number | null)[]; take_profit: (number | null)[] }
}

export interface StockLevels {
  levels: Record<LevelType, PriceLevel[]>
  close: number | null
  summary: string
  symbol: string
  /** dates 与 series 对齐;前端按自身 rows 的日期映射,缺失填 null */
  dates?: string[]
  series?: LevelSeries
}

export interface AiStockReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  summary?: string
  close?: number | null
  levels?: Record<LevelType, PriceLevel[]>
  created_at: string
}

// ===== Kline =====
export interface MinuteKlineRow {
  datetime: string | null
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface KlineRow {
  symbol?: string
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  change_pct?: number
  ma5?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_14?: number | null
  vol_ratio_5d?: number | null
  expma_12?: number | null
  expma_50?: number | null
  trix?: number | null
  trix_ma?: number | null
  bbi?: number | null
  dfma_dif?: number | null
  dfma?: number | null
  dmi_pdi?: number | null
  dmi_mdi?: number | null
  dmi_adx?: number | null
  dmi_adxr?: number | null
  xsii_upper?: number | null
  xsii_lower?: number | null
  xsii_mid?: number | null
  wr_14?: number | null
  cci_14?: number | null
  psy_12?: number | null
  psyma_6?: number | null
  bias_6?: number | null
  bias_12?: number | null
  bias_24?: number | null
  roc_12?: number | null
  roc_ma_6?: number | null
  mtm_12?: number | null
  mtm_ma_6?: number | null
  dpo_20?: number | null
  dpo_ma_6?: number | null
  ktn_mid?: number | null
  ktn_upper?: number | null
  ktn_lower?: number | null
  taq_mid?: number | null
  taq_upper?: number | null
  taq_lower?: number | null
  obv?: number | null
  vr_26?: number | null
  emv_14?: number | null
  emv_ma_14?: number | null
  mfi_14?: number | null
  cr_26?: number | null
  mass_9_25?: number | null
  asi?: number | null
  [key: string]: any
}

// ===== Watchlist =====
export interface WatchlistEntry {
  symbol: string
  added_at: string
  note?: string
  name?: string | null
}

export interface WatchlistImportCandidate {
  symbol: string
  source_text: string | null
}

export interface WatchlistImportResult {
  available: boolean
  candidates: WatchlistImportCandidate[]
  error?: string
}

export interface Quote {
  symbol: string
  price?: number
  pct?: number
  close?: number
  change_pct?: number
  [key: string]: any
}

export interface IndexInstrument {
  symbol: string
  name?: string | null
  code?: string | null
  asset_type?: 'index'
  [key: string]: any
}

export interface IndexQuote {
  symbol: string
  name?: string | null
  last_price?: number | null
  close?: number | null
  prev_close?: number | null
  change_pct?: number | null
  change_amount?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  volume?: number | null
  amount?: number | null
  timestamp?: number | null
  [key: string]: any
}

// ===== Screener =====
export interface ScreenerStrategy {
  id: string
  name: string
  description: string
  source?: string
}

export interface ScreenerResult {
  as_of: string
  strategy: string | null
  rows: any[]
  total: number
  elapsed_ms: number
}

export interface ScreenerCondition {
  field: string
  op: string
  value?: number | string | boolean | Array<number | string> | null
}

export interface ScreenerOrderBy {
  field: string
  direction: 'asc' | 'desc'
}

export interface ScreenerFieldSpec {
  field: string
  label: string
  group: string
  source: string
  unit?: string | null
  value_type: 'numeric' | 'enum' | 'boolean'
  null_policy: string
  availability: 'available' | 'unavailable'
  ops: string[]
  sortable: boolean
  options?: { value: string; label: string }[] | null
}

export interface ScreenerQueryRequest {
  conditions: ScreenerCondition[]
  as_of?: string
  order_by?: ScreenerOrderBy
  limit: number
}

export interface ScreenerQueryResponse {
  rows: Record<string, unknown>[]
  total: number
  applied: ScreenerCondition[]
  as_of: string | null
  elapsed_ms: number
}

export interface ScreenerNlUnrecognized {
  raw: string
  reason: string
}

export interface ScreenerNlParseResponse {
  recognized: ScreenerCondition[]
  unrecognized: ScreenerNlUnrecognized[]
  /** P3: 执行元信息(实际 profile/模型/fallback/usage),旧响应可缺失 */
  ai_meta?: AiExecutionMeta | null
}

export interface ScreenerPreset {
  id: string
  name: string
  description: string
  predicate: {
    conditions: ScreenerCondition[]
    order_by: ScreenerOrderBy | null
  }
  executable_level: 'full' | 'needs_fundamental' | 'unsupported'
}

export interface MarketSnapshotRow {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  amount?: number | null
  volume?: number | null
  turnover_rate?: number | null
  vol_ratio_5d?: number | null
  total_shares?: number | null
  float_shares?: number | null
  market_cap?: number | null
  float_market_cap?: number | null
  consecutive_limit_ups?: number | null
  [key: string]: any
}

export interface OverviewDimensionRankItem {
  name: string
  count: number
  avg_pct: number
  up_count: number
  down_count: number
  amount: number
  leader?: {
    symbol?: string | null
    name?: string | null
    change_pct?: number | null
  } | null
}

export interface OverviewMarket {
  as_of: string | null
  quote_status: {
    enabled?: boolean
    running?: boolean
    quote_age_ms?: number | null
    is_trading_hours?: boolean
    [key: string]: any
  }
  indices: IndexQuote[]
  breadth: {
    total: number
    up: number
    down: number
    flat: number
    up_pct: number
    down_pct: number
    avg_pct?: number | null
    median_pct?: number | null
    strong_up?: number
    strong_down?: number
  }
  amount: { total: number; avg: number }
  boards: { board: string; count: number; up: number; down: number; up_pct: number; amount: number }[]
  limit: { limit_up: number; broken: number; failed: number; limit_down: number; max_boards: number; seal_rate?: number; tiers: { boards: number; count: number }[]; sealed_ready?: boolean; fake_up?: number; fake_down?: number }
  distribution: { label: string; count: number; pct: number }[]
  trend: { above_ma5: number; above_ma20: number; above_ma60: number; above_ma5_pct: number; above_ma20_pct: number; above_ma60_pct: number; new_high: number; new_low: number }
  activity: { avg_turnover: number; high_turnover: number; high_vol_ratio: number; vol_ratio: number }
  radar: { key: string; label: string; value: number }[]
  emotion: { score: number; label: string }
  top_gainers: MarketSnapshotRow[]
  top_losers: MarketSnapshotRow[]
  turnover_leaders: MarketSnapshotRow[]
  active_leaders: MarketSnapshotRow[]
  concept_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
  industry_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
}

// ===== 概念涨幅轮动矩阵 =====
// dates: 日期字符串列表(最新在最前); columns: {日期: [[概念名, 涨幅小数], ...]} 每列各自降序
export interface RpsRotationData {
  dates: string[]
  columns: Record<string, [string, number][]>
  concept_count: number
}

// ===== 大盘复盘 =====
export interface AiReviewReport {
  id: string
  as_of: string
  focus?: string
  content: string
  summary?: string
  emotion_score?: number | null
  emotion_label?: string
  created_at: string
}

// ===== 复盘数据分区(/api/review/*) =====
// 单位约定(后端 services/review_series 已统一):
//   *_rate / *_change / change_pct 均为【百分数】(5.0 = 5%),直接 toFixed,不要再 *100。
//   amount 为元,用 fmtBigNum 格式化。

/** 情绪周期 / 连板天梯共用的逐日读数 */
export interface ReviewDailyPoint {
  trade_date: string
  total_amount: number | null
  amount_change_rate: number | null
  up_count: number
  down_count: number
  flat_count: number
  down_more_than_7_count: number
  limit_up_count: number
  limit_down_count: number
  break_count: number
  seal_rate: number | null
  max_board_count: number
  connected_board_count: number
  avg_change: number | null
  board_1: number
  board_2: number
  board_3: number
  board_4: number
  board_5: number
  high_board: number
  // 仅 /ladder 返回(晋级率由板层分布跨日派生)
  promotion_rate?: number | null
  first_to_second_rate?: number | null
  second_to_third_rate?: number | null
  third_to_fourth_rate?: number | null
  fourth_to_fifth_rate?: number | null
  fifth_to_high_rate?: number | null
}

export interface ReviewEmotion {
  as_of: string | null
  days: number
  trade_dates?: string[]
  series: ReviewDailyPoint[]
}

export interface ReviewLadder {
  as_of: string | null
  days: number
  high_board_from?: number
  series: ReviewDailyPoint[]
}

export interface ReviewRotationCell {
  trade_date: string
  name: string
  limit_up_count: number
  max_board_count: number
  amount: number | null
  avg_change: number | null
  leaders: { symbol: string; name: string | null; boards: number }[]
}

export interface ReviewRotation {
  as_of: string | null
  days: number
  available: boolean
  /** no_data = 无行情; no_concept_ext = 未配置概念扩展数据 */
  reason?: 'no_data' | 'no_concept_ext'
  themes: string[]
  trade_dates: string[]
  cells: ReviewRotationCell[]
}

export interface ReviewClueStock {
  symbol: string
  name: string | null
  close: number | null
  change_pct: number | null
  amount: number | null
  turnover_rate: number | null
  boards: number
  industry: string
  concepts: string[]
  /** 冲高回落专有 */
  high_pct?: number | null
  fade_pct?: number | null
  /** 反包专有 */
  prev_change_pct?: number | null
}

export interface ReviewClues {
  as_of: string | null
  trade_date?: string
  prev_date?: string | null
  broken: ReviewClueStock[]
  limit_down: ReviewClueStock[]
  surge_and_fade: ReviewClueStock[]
  top_amount: ReviewClueStock[]
  rebound: ReviewClueStock[]
}

// ===== 港股复盘分区(/api/review/hk/*) =====
// 港股无涨跌停制度,且 fstore 里港股只有 price/change_pct/volume/amount 四列有值
// (换手/高开低收/概念全为空)。所以港股是独立的、更薄的两个分区,不复用 A 股那四个。
// 单位同样是百分数。

export interface HkBreadthPoint {
  trade_date: string
  total: number
  total_amount: number | null
  amount_change_rate: number | null
  up_count: number
  down_count: number
  flat_count: number
  up_pct: number | null
  /** 涨/跌超 strong_pct(默认 5%) 的家数 —— 港股无涨跌停,用它替代"涨停/跌停"读数 */
  strong_up: number
  strong_down: number
  avg_change: number | null
  median_change: number | null
}

export interface HkBreadth {
  as_of: string | null
  days: number
  strong_pct?: number
  series: HkBreadthPoint[]
}

export interface HkMoverStock {
  symbol: string
  name: string | null
  board: string
  close: number | null
  change_pct: number | null
  amount: number | null
}

export interface HkBoardStat {
  board: string
  count: number
  up: number
  down: number
  up_pct: number | null
  amount: number | null
  avg_change: number | null
}

export interface HkMovers {
  as_of: string | null
  trade_date: string | null
  top_gainers: HkMoverStock[]
  top_losers: HkMoverStock[]
  top_amount: HkMoverStock[]
  boards: HkBoardStat[]
  distribution: { label: string; count: number; pct: number }[]
}

// ===== Strategy Engine =====
export interface StrategyParamDef {
  id: string
  label: string
  type: 'float' | 'int' | 'select' | 'bool'
  default: number | string | boolean
  min?: number
  max?: number
  step?: number
  options?: string[]
}

export interface StrategyDetail {
  id: string
  name: string
  description: string
  tags: string[]
  source: 'builtin' | 'custom' | 'ai'
  version: string
  basic_filter: Record<string, any>
  params: StrategyParamDef[]
  params_defaults: Record<string, any>
  scoring: Record<string, number>
  entry_signals: string[]
  exit_signals: string[]
  stop_loss: number | null
  take_profit: number | null
  trailing_stop: number | null
  trailing_take_profit_activate: number | null
  trailing_take_profit_drawdown: number | null
  max_hold_days: number | null
  display_limit?: number
  alerts: { field: string; op?: string; value?: number; message: string }[]
  order_by: string
  descending: boolean
  limit: number
}

// ===== Custom Signals (自定义信号) =====
export interface CustomSignalCondition {
  left: string     // 字段名
  op: string       // > >= < <= == !=
  right: string    // "field:xxx" 或数字字符串
}

export interface CustomSignal {
  id: string
  name: string
  kind: 'entry' | 'exit' | 'both'
  conditions: CustomSignalCondition[]
  enabled: boolean
}

export interface CustomSignalOptions {
  fields: { key: string; label: string }[]
  operators: string[]
  kinds: { key: string; label: string }[]
}

// ===== Monitor (监控规则 + 触发记录) =====
export interface MonitorCondition {
  field: string
  op: string              // truth | > >= < <= == !=
  value?: number | null   // op 非 truth 时必填
}

export interface MonitorRule {
  id: string
  name: string
  enabled: boolean
  type: 'strategy' | 'signal' | 'price' | 'market'
  scope: 'symbols' | 'all' | 'sector'
  symbols: string[]
  sector?: string | null
  asset_type?: 'stock' | 'etf' | 'index'
  strategy_id?: string | null
  direction: 'entry' | 'exit' | 'both'
  conditions: MonitorCondition[]
  logic: 'and' | 'or'
  cooldown_seconds: number
  severity: 'info' | 'warn' | 'critical'
  message: string
  webhook_url?: string
  webhook_enabled?: boolean
  created_at?: string
}

export interface MonitorRuleOptions {
  threshold_fields: { key: string; label: string }[]
  builtin_signals: { key: string; label: string }[]
  custom_signals: { key: string; label: string }[]
  operators: string[]
  types: { key: string; label: string }[]
  scopes: { key: string; label: string }[]
  logics: { key: string; label: string }[]
  severities: { key: string; label: string }[]
  directions: { key: string; label: string }[]
}

export interface AlertEvent {
  ts: number
  rule_id?: string
  rule_name?: string
  source: string
  type: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  severity?: string
  strategy_id?: string
  conditions?: MonitorCondition[]
  logic?: 'and' | 'or'
}

/** 生成监控规则 id (时间戳 + 随机后缀), 用户无需手动填写。 */
export function genRuleId(): string {
  const ts = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 6)
  return `mr_${ts}_${rand}`
}

// ===== Limit Ladder =====
export interface LimitLadderStock {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  consecutive_limit_ups?: number | null
  consecutive_limit_downs?: number | null
  status?: 'limit_up' | 'broken' | 'failed' | 'limit_down' | 'recovery' | null
  /** 五档 sealed: real=真封板, fake=假涨停(已归炸板), pending=待确认, null=降级/无能力 */
  sealed_status?: 'real' | 'fake' | 'pending' | null
  /** 封单量(买一/卖一量), 仅真封板有值 */
  sealed_vol?: number | null
}

export interface LimitLadderTier {
  boards: number
  count: number
  stocks: LimitLadderStock[]
}

export interface LimitLadderResult {
  as_of: string
  tiers: LimitLadderTier[]
  /** 双方向涨跌停计数(修正后, 不论当前 direction) */
  counts?: { up: number; down: number }
  /** 双方向涨跌停原始计数(修正前, 供弹窗对比) */
  counts_raw?: { up: number; down: number }
  /** sealed 数据是否就绪(false→前端显示降级标识) */
  sealed_ready?: boolean
  /** sealed 数据 age(秒), null=盘后定版或无数据 */
  sealed_age?: number | null
  /** sealed 修正统计: real=真封板, fake=假涨停(归炸板), pending=待确认 */
  sealed_counts?: { real: number; fake: number; pending: number }
  /** 涨停侧 sealed 明细 */
  sealed_counts_up?: { real: number; fake: number; pending: number }
  /** 跌停侧 sealed 明细 */
  sealed_counts_down?: { real: number; fake: number; pending: number }
}

// ===== Backtest =====
export interface BacktestResult {
  run_id: string
  config: any
  stats: Record<string, any>
  equity_curve: { date: string; value: number }[]
  trades: any[]
  per_symbol_stats: { symbol: string; total_return: number }[]
  methodology_context?: string
  warnings?: string[]
}

export type OptimizeMethod =
  | 'equal'
  | 'equal_vol'
  | 'risk_parity'
  | 'mean_variance'
  | 'max_diversification'
  | 'score_weight'

export interface OptimizeWeight {
  symbol: string
  name?: string | null
  weight: number
}

export interface OptimizeResult {
  weights: OptimizeWeight[]
  stats: { n: number; annualized_vol: number | null; diversification_ratio: number | null }
  method: OptimizeMethod
  lookback_days: number
  meta: { kept: string[]; dropped: string[] }
}

// ===== Factor Backtest =====
export interface FactorColumn {
  id: string
  label: string
  group: string
  desc: string
}

export interface GroupStat {
  group: number
  label: string
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  win_rate: number
}

export interface FactorBacktestResult {
  run_id: string
  config: Record<string, any>
  ic_mean: number | null
  ic_std: number | null
  ir: number | null
  ic_win_rate: number | null
  ic_series: { date: string; ic: number }[]
  group_stats: GroupStat[]
  group_nav: Record<string, any>[]
  long_short_stats: Record<string, any>
  long_short_nav: { date: string; value: number }[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
  methodology_context?: string
  warnings?: string[]
}

// ===== Strategy Backtest =====
export interface StrategyBacktestTrade {
  symbol: string
  name?: string
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  pnl_pct: number
  duration: number
  exit_reason: string
  shares?: number
  lots?: number
  position_pct?: number
  entry_value?: number
  exit_value?: number
  pnl_amount?: number
  entry_score?: number | null
  entry_signal_date?: string | null
  exit_signal_date?: string | null
  blocked_exit_days?: number
}

export interface StrategyBacktestResult {
  run_id: string
  config: Record<string, any>
  stats: Record<string, any>
  equity_curve: { date: string; value: number; cash?: number; positions?: number; exposure?: number }[]
  drawdown_curve: { date: string; value: number }[]
  benchmark_curve?: { date: string; value: number; close?: number; name?: string; symbol?: string }[]
  trades: StrategyBacktestTrade[]
  per_symbol_stats: {
    symbol: string
    n_trades: number
    total_return: number
    win_rate: number
    best: number
    worst: number
  }[]
  strategy_info: {
    id: string
    name: string
    description: string
    entry_signals: string[]
    exit_signals: string[]
    stop_loss: number | null
    take_profit: number | null
    trailing_stop: number | null
    trailing_take_profit_activate: number | null
    trailing_take_profit_drawdown: number | null
    score_min: number | null
    score_max: number | null
    max_hold_days: number | null
    source: string
  }
  elapsed_ms: number
  error: string | null
  methodology_context?: string
  warnings?: string[]
}

// ===== Settings =====

export interface SettingsState {
  mode: 'fquant' | 'fquant_local'
  data_provider: 'fquant' | 'fquant_local'
  // 首次使用引导
  onboarding_completed: boolean
  // AI 配置
  ai_provider: string
  ai_base_url: string
  ai_api_key_masked: string
  has_ai_key: boolean
  ai_configured?: boolean
  ai_model: string
  ai_codex_command?: string
  ai_user_agent: string
}

export type AiProviderKind = 'openai_compat' | 'acp' | 'codex_cli'

export interface AiProfileMasked {
  id: string
  name: string
  provider: AiProviderKind | string
  base_url?: string
  model?: string
  codex_command?: string
  launch_command?: string
  user_agent?: string
  has_api_key: boolean
  api_key_masked?: string
  is_default: boolean
  available?: boolean
}

export interface AiProfileInput {
  name: string
  provider: AiProviderKind | string
  base_url?: string
  api_key?: string
  model?: string
  codex_command?: string
  launch_command?: string
  user_agent?: string
}

/** AI 路由策略 — 备用 profile 受控 fallback(默认关闭,仅 provider 故障时按序切换) */
export interface AiRoutePolicy {
  allow_profile_fallback: boolean
  fallback_profile_ids: string[]
}

/**
 * P3: token 用量 — 原生 prompt-cache 可观测。
 * 全部 optional;provider 不上报时字段缺失,前端不得展示伪数据(尤其 cached_prompt_tokens)。
 */
export interface AiUsageMeta {
  prompt_tokens?: number
  cached_prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

/**
 * P3: AI 执行元信息 — 结构化入口(nl_screener / strategy_profile_deep_review / trading_autopsy)
 * 与个股流 meta 统一携带的 `ai_meta` 对象。全部 optional,旧响应可缺失整个字段。
 * fallback 默认关闭;开启时仅在 provider/quota/auth/timeout 类故障按序切换。
 */
export interface AiExecutionMeta {
  /** 用户/入口最初选中的 profile */
  primary_profile_id?: string | null
  /** 实际执行的 profile(fallback 后与 primary 不同) */
  profile_id?: string | null
  fallback_used?: boolean
  fallback_reason?: string | null
  provider?: string
  model?: string
  usage?: AiUsageMeta | null
}

export interface AiProfilesResponse {
  profiles: AiProfileMasked[]
  default_id: string
  route_policy: AiRoutePolicy
}

export interface Preferences {
  data_provider?: string
  effective_data_provider?: string
  data_provider_env_override?: boolean
  realtime_quotes_enabled: boolean
  realtime_allowed?: boolean
  indices_nav_pinned: boolean
  minute_sync_enabled: boolean
  minute_sync_days: number
  daily_data_provider?: string
  adj_factor_provider?: string
  minute_data_provider?: string
  realtime_data_provider?: string
  financial_data_provider?: string
  depth_data_provider?: string
  realtime_watchlist_symbols?: string[]
  realtime_pull_stock?: boolean
  realtime_pull_etf?: boolean
  realtime_pull_index?: boolean
  realtime_index_mode?: 'core' | 'all'
  realtime_index_symbols?: string[]
  pipeline_pull_a_share: boolean
  pipeline_pull_etf: boolean
  pipeline_pull_index: boolean
  pipeline_pull_hk: boolean
  pipeline_index_symbols: string
  pipeline_schedule: { hour: number; minute: number }
  instruments_schedule: { hour: number; minute: number }
  enriched_batch_size: number
  index_daily_batch_size: number
  limit_ladder_monitor_enabled: boolean
  depth_polling_interval: number
  depth_finalize_time: { hour: number; minute: number }
  review_schedule: { enabled: boolean; hour: number; minute: number }
  review_push_channels: string[]
  sse_refresh_pages: Record<string, boolean>
  strategy_monitor_enabled: boolean
  strategy_monitor_ids: string[]
  system_notify_enabled: boolean
  feishu_webhook_url?: string
  feishu_webhook_secret?: string
  webhook_channels?: Record<string, { url?: string; secret?: string; nickname?: string; token?: string; configured?: boolean; token_masked?: string }>
  webhook_enabled_default?: boolean
  sidebar_index_symbols: string[]
  nav_order: string[]
  nav_hidden: string[]
  screener_auto_run: boolean
  tradingAutoReview: boolean
  structured_plan_check_enabled?: boolean
}

// ===== Strategy Alert =====
export interface StrategyAlertEvent {
  source: 'strategy' | 'depth'
  type: string
  strategy_id?: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
}

// ===== Trade Journal =====
export interface JournalPreview {
  sheets: string[]
  columns: string[]
  guessed_mapping: Record<string, string>
  preview_rows: Record<string, any>[]
  row_count: number
  warnings: string[]
}

export interface JournalTrip {
  account_id?: string
  symbol: string
  name: string
  open_date: string
  close_date: string
  qty: number
  buy_avg: number
  sell_avg: number
  pnl: number
  total_pnl: number
  pnl_pct: number
  fees: number
  dividend: number
  holding_days: number
  benchmark_pct?: number | null
  excess?: number | null
}

export interface JournalLedger {
  imported_at: string
  accounts?: { id: string; fills: number }[]
  import?: {
    mode: 'replace' | 'append'
    account_id: string
    new_fills: number
    deduped_fills: number
    deduped_events: number
  }
  trips: JournalTrip[]
  summary: {
    total_trips: number
    win_trips: number
    total_pnl: number
    total_dividend: number
    total_fees: number
    win_rate: number
    avg_win: number
    avg_loss: number
    profit_factor: number
    open_positions: Record<string, any>[]
  }
  diagnosis: Record<string, any>
  benchmark: {
    code: string
    name: string
    account: { account_return: number | null; benchmark_return: number | null; excess: number | null; window: string[] | null }
    per_trip: (Pick<JournalTrip, 'account_id' | 'symbol' | 'open_date' | 'close_date' | 'pnl_pct'> & { benchmark_pct: number | null; excess: number | null })[]
    noise_note: string
  }
  narrative?: string
  methodology_context?: string
  warnings: string[]
}

export interface JournalPresets {
  presets: { id: string; label: string; sheet: string; mapping: Record<string, string> }[]
  benchmarks: { symbol: string; name: string }[]
}

// ===== API surface =====
export const api = {
  health: () => request<{ status: string; version: string; mode: string }>('/health'),

  // ===== Auth (访问认证) =====
  authStatus: () =>
    request<{ configured: boolean; authenticated: boolean }>('/api/auth/status'),
  authSetup: (password: string) =>
    request<{ ok: boolean }>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogin: (password: string) =>
    request<{ ok: boolean }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogout: () =>
    request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  authChangePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),

  journalPresets: () => request<JournalPresets>('/api/journal/presets'),
  journalLedger: () => request<JournalLedger>('/api/journal/ledger'),
  journalDelete: () => request<{ deleted: boolean }>('/api/journal/ledger', { method: 'DELETE' }),
  journalFeedback: (rating: 'helpful' | 'not_helpful') =>
    request<{ ok: boolean }>('/api/journal/feedback', {
      method: 'POST',
      body: JSON.stringify({ rating }),
    }),
  journalUpload: (
    file: File,
    commit: boolean,
    mapping?: Record<string, string>,
    sheet?: string,
    benchmark?: string,
    accountId?: string,
    append?: boolean,
    narrative?: boolean,
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    if (mapping) fd.append('mapping', JSON.stringify(mapping))
    if (sheet) fd.append('sheet', sheet)
    if (benchmark) fd.append('benchmark', benchmark)
    if (accountId) fd.append('account_id', accountId)
    fd.append('append', String(!!append))
    fd.append('narrative', String(!!narrative))
    return request<JournalPreview | JournalLedger>(`/api/journal/upload?commit=${commit}`, { method: 'POST', body: fd })
  },

  settings: () => request<SettingsState>('/api/settings'),

  /** 标记首次使用向导完成（持久化到后端 preferences） */
  completeOnboarding: () =>
    request<{ ok: boolean; onboarding_completed: boolean }>(
      '/api/settings/onboarding/complete', { method: 'POST' },
    ),

  /** 保存 AI 配置 */
  saveAiSettings: (ai: { provider?: string; base_url?: string; api_key?: string; model?: string; codex_command?: string; user_agent?: string }) =>
    request<{ ok: boolean; ai_provider?: string; ai_model?: string; ai_codex_command?: string; ai_configured?: boolean }>('/api/settings/ai', {
      method: 'POST',
      body: JSON.stringify(ai),
    }),

  /** 一键清空 AI 配置(保留自定义 UA) */
  clearAiSettings: () =>
    request<{ ok: boolean }>('/api/settings/ai', { method: 'DELETE' }),

  aiProfiles: () =>
    request<AiProfilesResponse>('/api/settings/ai/profiles'),

  updateAiRoutePolicy: (policy: AiRoutePolicy) =>
    request<{ route_policy: AiRoutePolicy }>('/api/settings/ai/route-policy', {
      method: 'PUT',
      body: JSON.stringify(policy),
    }),

  createAiProfile: (profile: AiProfileInput) =>
    request<{ id: string }>('/api/settings/ai/profiles', {
      method: 'POST',
      body: JSON.stringify(profile),
    }),

  updateAiProfile: (id: string, profile: Partial<AiProfileInput>) =>
    request<{ ok: boolean }>(`/api/settings/ai/profiles/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(profile),
    }),

  deleteAiProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/settings/ai/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  setDefaultAiProfile: (id: string) =>
    request<{ ok: boolean }>(`/api/settings/ai/profiles/${encodeURIComponent(id)}/default`, { method: 'POST' }),

  testAiProfile: (id: string) =>
    request<{ ok: boolean; error?: string; model?: string; response?: string }>(
      `/api/settings/ai/profiles/${encodeURIComponent(id)}/test`,
      { method: 'POST' },
    ),

  agentTools: () => request<{ tools: AgentTool[] }>('/api/agent/tools'),
  agentSessions: () => request<{ sessions: AgentSession[] }>('/api/agent/sessions'),
  createAgentSession: (title?: string) =>
    request<AgentSession>('/api/agent/sessions', { method: 'POST', body: JSON.stringify({ title: title ?? '' }) }),
  renameAgentSession: (sessionId: string, title: string) =>
    request<AgentSession>(`/api/agent/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),
  deleteAgentSession: (sessionId: string) =>
    request<{ deleted: boolean }>(`/api/agent/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
  agentSessionMessages: (sessionId: string) =>
    request<{ messages: AgentStoredMessage[] }>(`/api/agent/sessions/${encodeURIComponent(sessionId)}/messages`),
  cancelAgentAttempt: (attemptId: string) =>
    request<{ cancelled: boolean }>(`/api/agent/attempts/${encodeURIComponent(attemptId)}/cancel`, { method: 'POST' }),
  readDocument: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<DocumentEnvelope>('/api/documents/read', { method: 'POST', body: fd })
  },

  agentSend: (sessionId: string, messages: AgentMsg[], profileId?: string) =>
    request<{ attempt_id: string; session_id: string }>(
      `/api/agent/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: 'POST',
        body: JSON.stringify({
          messages,
          ...(profileId ? { profile_id: profileId } : {}),
        }),
      },
    ),

  async *agentWatch(sessionId: string, signal?: AbortSignal): AsyncGenerator<AgentEvent> {
    const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}/stream`, { signal })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) as AgentEvent } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) as AgentEvent } catch { /* ignore */ }
    }
  },

  preferences: () => request<Preferences>('/api/settings/preferences'),
  updateDataProvider: (data_provider: 'fquant' | 'fquant_local') =>
    request<{
      data_provider: string
      effective_data_provider: string
      data_provider_env_override: boolean
      mode: string
      tier_label: string
      realtime_allowed: boolean
    }>('/api/settings/preferences/data-provider', {
      method: 'PUT',
      body: JSON.stringify({ data_provider }),
    }),
  updateMinuteSync: (enabled: boolean, days: number) =>
    request<Preferences>('/api/settings/preferences/minute-sync', {
      method: 'PUT',
      body: JSON.stringify({ minute_sync_enabled: enabled, minute_sync_days: days }),
    }),
  updatePipelinePullTypes: (cfg: Partial<Pick<Preferences, 'pipeline_pull_a_share' | 'pipeline_pull_etf' | 'pipeline_pull_index' | 'pipeline_pull_hk'>>) =>
    request<{
      pipeline_pull_a_share: boolean
      pipeline_pull_etf: boolean
      pipeline_pull_index: boolean
      pipeline_pull_hk: boolean
    }>('/api/settings/preferences/pipeline-pull-types', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updatePipelineIndexSymbols: (symbols: string) =>
    request<{ pipeline_index_symbols: string }>('/api/settings/preferences/pipeline-index-symbols', {
      method: 'PUT',
      body: JSON.stringify({ symbols }),
    }),
  updateRealtimeQuotes: (enabled: boolean) =>
    request<{ realtime_quotes_enabled: boolean; realtime_allowed?: boolean; mode?: string; error?: string }>('/api/settings/preferences/realtime-quotes', {
      method: 'PUT',
      body: JSON.stringify({ realtime_quotes_enabled: enabled }),
    }),
  updateRealtimeQuoteScope: (cfg: Partial<Pick<Preferences, 'realtime_pull_stock' | 'realtime_pull_etf' | 'realtime_pull_index' | 'realtime_index_mode' | 'realtime_index_symbols'>>) =>
    request<Partial<Preferences>>('/api/settings/preferences/realtime-quote-scope', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateIndicesNavPinned: (pinned: boolean) =>
    request<{ indices_nav_pinned: boolean }>('/api/settings/preferences/indices-nav-pinned', {
      method: 'PUT',
      body: JSON.stringify({ indices_nav_pinned: pinned }),
    }),
  quoteStatus: () =>
    request<{
      enabled: boolean
      running: boolean
      mode?: 'none' | 'watchlist' | 'full_market'
      realtime_allowed?: boolean
      interval_s: number
      symbol_count: number
      watchlist_symbol_count?: number
      index_symbol_count?: number
      etf_symbol_count?: number
      quote_age_ms: number | null
      is_trading_hours: boolean
      last_fetch_ms: number | null
    }>('/api/intraday/status'),
  quoteInterval: () =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
    ),
  updateQuoteInterval: (interval: number) =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
      { method: 'PUT', body: JSON.stringify({ interval }) },
    ),
  intradayRefresh: () => request<{ status: string }>('/api/intraday/refresh', { method: 'POST' }),
  indexQuotes: (symbols?: string[]) =>
    request<{ rows: IndexQuote[]; count: number }>(
      `/api/intraday/indices${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`,
    ),
  updateRealtimeMonitorConfig: (cfg: {
    sse_refresh_pages?: Record<string, boolean>
    strategy_monitor_enabled?: boolean
    strategy_monitor_ids?: string[]
    sidebar_index_symbols?: string[]
    screener_auto_run?: boolean
  }) =>
    request<{
      sse_refresh_pages: Record<string, boolean>
      strategy_monitor_enabled: boolean
      strategy_monitor_ids: string[]
      sidebar_index_symbols: string[]
      screener_auto_run: boolean
    }>('/api/settings/preferences/realtime-monitor', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateSystemNotify: (enabled: boolean) =>
    request<{ system_notify_enabled: boolean }>('/api/settings/preferences/system-notify', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateFeishuWebhook: (url: string, secret: string = '') =>
    request<{ feishu_webhook_url: string; feishu_webhook_secret: string }>('/api/settings/preferences/feishu-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url, secret }),
    }),
  updateWebhookChannel: (channel: string, config: { url?: string; secret?: string; nickname?: string; token?: string; clear_token?: boolean }) =>
    request<{ webhook_channels: Preferences['webhook_channels'] }>('/api/settings/preferences/webhook-channel', {
      method: 'PUT',
      body: JSON.stringify({ channel, ...config }),
    }),
  updateWebhookDefault: (enabled: boolean) =>
    request<{ webhook_enabled_default: boolean }>('/api/settings/preferences/webhook-enabled-default', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updatePipelineSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/pipeline-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateReviewSchedule: (enabled: boolean, hour: number, minute: number) =>
    request<{ enabled: boolean; hour: number; minute: number }>('/api/settings/preferences/review-schedule', {
      method: 'PUT',
      body: JSON.stringify({ enabled, hour, minute }),
    }),
  updateReviewPush: (channels: string[]) =>
    request<{ review_push_channels: string[] }>('/api/settings/preferences/review-push', {
      method: 'PUT',
      body: JSON.stringify({ channels }),
    }),
  updateTradingAutoReview: (enabled: boolean) =>
    request<{ tradingAutoReview: boolean }>('/api/settings/preferences/trading-auto-review', {
      method: 'PUT',
      body: JSON.stringify({ tradingAutoReview: enabled }),
    }),
  updateStructuredPlanCheck: (enabled: boolean) =>
    request<{ structured_plan_check_enabled: boolean }>('/api/settings/preferences/structured-plan-check', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateDepthPollingInterval: (interval: number) =>
    request<{ depth_polling_interval: number }>('/api/settings/preferences/depth-polling-interval', {
      method: 'PUT',
      body: JSON.stringify({ interval }),
    }),
  updateLimitLadderMonitor: (enabled: boolean) =>
    request<{ limit_ladder_monitor_enabled: boolean }>('/api/settings/preferences/limit-ladder-monitor', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  runLimitLadderFix: () =>
    request<{ ok: boolean; count: number; msg: string }>('/api/settings/preferences/limit-ladder-monitor/run', {
      method: 'POST',
    }),
  updateDepthFinalizeTime: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/depth-finalize-time', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  saveNavOrder: (nav_order: string[]) =>
    request<{ nav_order: string[] }>('/api/settings/preferences/nav-order', {
      method: 'PUT',
      body: JSON.stringify({ nav_order }),
    }),
  saveNavHidden: (nav_hidden: string[]) =>
    request<{ nav_hidden: string[] }>('/api/settings/preferences/nav-hidden', {
      method: 'PUT',
      body: JSON.stringify({ nav_hidden }),
    }),
  updateInstrumentsSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/instruments-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateEnrichedBatchSize: (size: number) =>
    request<{ enriched_batch_size: number }>('/api/settings/preferences/enriched-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),
  updateIndexDailyBatchSize: (size: number) =>
    request<{ index_daily_batch_size: number }>('/api/settings/preferences/index-daily-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),

  // 自选列表列配置
  watchlistColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/watchlist-columns'),
  updateWatchlistColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/watchlist-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  // 策略结果列表列配置
  screenerResultColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/screener-result-columns'),
  updateScreenerResultColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/screener-result-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  capabilities: () => request<CapabilitiesResponse>('/api/capabilities'),
  version: () => request<{ version: string }>('/api/data/version'),
  redetectCapabilities: () =>
    request<CapabilitiesResponse>('/api/capabilities/redetect', { method: 'POST' }),

  klineDaily: (symbol: string, days = 120, dateRange?: { start: string; end: string }, extColumns?: string) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number; ext?: Record<string, unknown> }
      rows: KlineRow[]
      source?: string
      adjustment?: string
    }>(
      (dateRange
        ? `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`)
      + (extColumns ? `&ext_columns=${encodeURIComponent(extColumns)}` : ''),
    ),
  klineDailyBatch: (symbols: string[], days = 12) =>
    request<{ data: Record<string, KlineRow[]> }>('/api/kline/daily-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, days }),
    }),
  instrumentSearch: (q: string, limit = 20) =>
    request<{ results: InstrumentSearchResult[] }>(
      `/api/kline/instruments/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  /** 批量查股票名称 (传入 symbol 列表, 返回 {symbol: name}) */
  instrumentNames: (symbols: string[]) =>
    request<{ names: Record<string, string> }>('/api/kline/instruments/names', {
      method: 'POST',
      body: JSON.stringify(symbols),
    }),
  klineMinute: (symbol: string, date?: string) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number }
      asset_type?: 'stock' | 'etf' | 'index'
      date: string | null
      rows: MinuteKlineRow[]
      source?: 'local' | 'local_disk' | 'tdx_api' | 'live' | 'none'
    }>(
      `/api/kline/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}`,
    ),
  indexList: () => request<{ results: IndexInstrument[]; count: number }>('/api/index/list'),
  indexSearch: (q: string, limit = 20) =>
    request<{ results: IndexInstrument[] }>(
      `/api/index/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  indexDaily: (symbol: string, days = 120, dateRange?: { start: string; end: string }) =>
    request<{
      symbol: string
      name?: string
      index_info?: IndexInstrument
      rows: KlineRow[]
      source?: string
    }>(
      dateRange
        ? `/api/index/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/index/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  indexMinute: (symbol: string, date?: string) =>
    request<{
      symbol: string
      name?: string
      index_info?: IndexInstrument
      date: string | null
      rows: MinuteKlineRow[]
      source?: string
    }>(
      `/api/index/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}`,
    ),
  syncIndexInstruments: () =>
    request<{ status: string; count: number }>('/api/index/sync_instruments', { method: 'POST' }),
  syncIndexDaily: (days = 365) =>
    request<{ status: string; index_count: number; rows_written: number }>(
      `/api/index/sync_daily?days=${days}`,
      { method: 'POST' },
    ),
  syncSymbol: (symbol: string, days = 250) =>
    request<{ symbol: string; rows_written: number }>(
      `/api/kline/sync?symbol=${encodeURIComponent(symbol)}&days=${days}`,
      { method: 'POST' },
    ),
  syncMinute: () =>
    request<{ status: string; job_id: string }>('/api/kline/sync_minute', { method: 'POST' }),
  extendHistory: (value: number, unit: 'day' | 'month' | 'year') =>
    request<{ status: string; job_id: string }>('/api/kline/extend_history', {
      method: 'POST',
      body: JSON.stringify({ value, unit }),
    }),
  extendMinuteHistory: (value: number, unit: 'day' | 'month') =>
    request<{ status: string; job_id: string }>('/api/kline/extend_minute_history', {
      method: 'POST',
      body: JSON.stringify({ value, unit }),
    }),
  rebuildEnriched: () =>
    request<{ status: string; job_id: string }>('/api/kline/rebuild_enriched', {
      method: 'POST',
    }),
  repairEnrichedRange: (startDate: string, endDate: string) =>
    request<{ status: string; job_id: string }>('/api/kline/repair_enriched_range', {
      method: 'POST',
      body: JSON.stringify({ start_date: startDate, end_date: endDate }),
    }),

  watchlistList: () => request<{ symbols: WatchlistEntry[] }>('/api/watchlist'),
  watchlistAdd: (symbol: string, note = '') =>
    request<{ symbols: WatchlistEntry[] }>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify({ symbol, note }),
    }),
  watchlistBatchAdd: (symbols: string[], note = '') =>
    request<{ symbols: WatchlistEntry[]; added: number }>('/api/watchlist/batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, note }),
    }),
  watchlistRemove: (symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  watchlistOcrStatus: () =>
    request<{ provider: string; available: boolean }>('/api/watchlist/ocr-status'),
  watchlistImportImage: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<WatchlistImportResult>('/api/watchlist/import-image', {
      method: 'POST',
      body: fd,
    })
  },
  watchlistMoveToTop: (symbol: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}/top`,
      { method: 'POST' },
    ),
  watchlistClear: () =>
    request<{ removed: number }>('/api/watchlist', { method: 'DELETE' }),
  watchlistQuotes: () => request<{ quotes: Quote[] }>('/api/watchlist/quotes'),
  watchlistEnriched: (extColumns?: string) =>
    request<{ rows: any[]; as_of: string | null; elapsed_ms: number }>(
      extColumns
        ? `/api/watchlist/enriched?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/watchlist/enriched',
    ),

  screenerStrategies: () => request<{ presets: ScreenerStrategy[] }>('/api/screener/strategies'),
  screenerFields: () => request<{ fields: ScreenerFieldSpec[] }>('/api/screener/fields'),
  screenerConditionQuery: (payload: ScreenerQueryRequest) =>
    request<ScreenerQueryResponse>('/api/screener/query', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  screenerNlParse: (text: string, profileId?: string) =>
    request<ScreenerNlParseResponse>('/api/screener/nl_parse', {
      method: 'POST',
      body: JSON.stringify({ text, ...(profileId ? { profile_id: profileId } : {}) }),
    }),
  screenerNlPresets: () => request<{ presets: ScreenerPreset[] }>('/api/screener/nl_presets'),
  screenerRunPreset: (strategy_id: string, pool?: string[], asOf?: string, extColumns?: string) =>
    request<ScreenerResult>('/api/screener/run_preset', {
      method: 'POST',
      body: JSON.stringify({ strategy_id, pool, as_of: asOf ?? null, ext_columns: extColumns || null }),
    }),
  screenerRunCustom: (conditions: string[], orderBy?: string, limit = 30, pool?: string[], extColumns?: string) =>
    request<ScreenerResult>('/api/screener/run', {
      method: 'POST',
      body: JSON.stringify({ conditions, order_by: orderBy, limit, pool, ext_columns: extColumns || null }),
    }),
  screenerRunAll: (asOf?: string, strategyIds?: string[], extColumns?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string; rows: any[] }> }>(
      '/api/screener/run_all', { method: 'POST', body: JSON.stringify({ as_of: asOf ?? null, strategy_ids: strategyIds ?? null, ext_columns: extColumns || null }) },
    ),
  screenerCached: (extColumns?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string; rows: any[] }>; today_ever_matched: Record<string, string[]> | null; today_ever_rows: Record<string, Record<string, any>> | null; updated_at: number | null }>(
      extColumns
        ? `/api/screener/cached?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/screener/cached',
    ),
  marketSnapshot: () =>
    request<{ as_of: string | null; rows: MarketSnapshotRow[] }>('/api/screener/market-snapshot'),
  overviewMarket: (asOf?: string) => request<OverviewMarket>(`/api/overview/market${asOf ? `?as_of=${asOf}` : ''}`),

  // 概念涨幅轮动矩阵: 每列(日期)各自把所有概念按当天涨幅从高到低排序
  rpsRotation: (days: number) =>
    request<RpsRotationData>(`/api/rps/rotation?days=${days}`),

  limitLadder: (asOf?: string, extColumns?: string, direction?: 'up' | 'down') => {
    const params = new URLSearchParams()
    if (asOf) params.set('as_of', asOf)
    if (extColumns) params.set('ext_columns', extColumns)
    if (direction === 'down') params.set('direction', 'down')
    const qs = params.toString()
    return request<LimitLadderResult>(
      `/api/screener/limit-ladder${qs ? `?${qs}` : ''}`,
    )
  },

  backtestStatus: () => request<{ available: boolean }>('/api/backtest/status'),

  backtestRun: (payload: {
    symbols: string[]
    entries: string[]
    exits: string[]
    start?: string
    end?: string
    stop_loss_pct?: number
    max_hold_days?: number
    matching?: 'close_t' | 'open_t+1'
  }) =>
    request<BacktestResult>('/api/backtest/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  optimize: (body: { symbols: string[]; method: OptimizeMethod; lookback_days?: number }) =>
    request<OptimizeResult>('/api/backtest/optimize', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  factorColumns: () =>
    request<{ columns: FactorColumn[] }>('/api/backtest/factor/columns'),

  factorRun: (payload: {
    factor_name: string
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    fees_pct?: number
    slippage_bps?: number
  }) =>
    request<FactorBacktestResult>('/api/backtest/factor/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  strategyBacktestRun: (payload: {
    strategy_id: string
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    params?: Record<string, any> | null
    overrides?: Record<string, any> | null
    matching?: 'close_t' | 'open_t+1'
    entry_fill?: 'close_t' | 'open_t+1' | null
    exit_fill?: 'close_t' | 'open_t+1' | null
    fees_pct?: number
    slippage_bps?: number
    max_positions?: number
    initial_capital?: number
    position_sizing?: 'equal' | 'score_weight' | 'equal_vol' | 'risk_parity' | 'mean_variance' | 'max_diversification'
  }) =>
    request<StrategyBacktestResult>('/api/backtest/strategy/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  pipelineRun: () => request<{ job_id: string; reused: boolean }>(
    '/api/pipeline/run', { method: 'POST' },
  ),
  pipelineJob: (id: string) => request<PipelineJob>(`/api/pipeline/jobs/${id}`),
  pipelineJobs: (limit = 20) =>
    request<{ active_id: string | null; jobs: PipelineJobSummary[] }>(
      `/api/pipeline/jobs?limit=${limit}`,
    ),

  dataStatus: () => request<DataStatus>('/api/data/status'),
  dataClear: () => request<{ deleted_files: number }>('/api/data/clear', { method: 'POST' }),
  enrichedSchema: (table: string) => request<EnrichedField[]>(`/api/data/schema/${table}`),

  testEndpoint: (url: string, rounds?: number) =>
    request<{
      ok: boolean
      url: string
      rounds: number
      success: number
      median_ms: number | null
      min_ms?: number | null
      max_ms?: number | null
      /** 兼容旧字段,等于 median_ms */
      latency_ms?: number | null
      error?: string
    }>(
      '/api/settings/test_endpoint', {
        method: 'POST',
        body: JSON.stringify({ url, rounds }),
      },
    ),

  // ===== 扩展数据 =====
  extDataList: () =>
    request<{ items: ExtDataConfig[] }>('/api/ext-data'),

  extDataRows: (id: string, opts?: { date?: string; limit?: number; columns?: string[] }) => {
    const qs = new URLSearchParams()
    if (opts?.date) qs.set('date', opts.date)
    if (opts?.limit) qs.set('limit', String(opts.limit))
    if (opts?.columns?.length) qs.set('columns', opts.columns.join(','))
    const suffix = qs.toString()
    return request<ExtDataRowsResult>(`/api/ext-data/${encodeURIComponent(id)}/rows${suffix ? `?${suffix}` : ''}`)
  },

  analysisMenus: () =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus'),

  analysisMenu: (id: string) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`),

  analysisMenuSave: (id: string, body: Omit<AnalysisMenu, 'id' | 'created_at' | 'updated_at' | 'builtin'>) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  analysisMenuReorder: (ids: string[]) =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus/reorder', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),

  analysisMenuDelete: (id: string) =>
    request<{ status: string }>(`/api/analysis-menus/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  extDataCreate: (body: { id: string; label: string; mode: 'snapshot' | 'timeseries'; fields: { name: string; dtype: string; label: string }[]; description?: string; symbol_map?: Record<string, string>; code_map?: Record<string, string> }) =>
    request<ExtDataConfig>('/api/ext-data', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  extDataUpdate: (id: string, body: { label?: string; fields?: { name: string; dtype: string; label: string }[]; description?: string }) =>
    request<ExtDataConfig>(`/api/ext-data/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  extDataDelete: (id: string) =>
    request<{ status: string }>(`/api/ext-data/${id}`, { method: 'DELETE' }),

  extDataUpload: (id: string, file: File, snapshotDate?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/upload${snapshotDate ? `?snapshot_date=${snapshotDate}` : ''}`,
      { method: 'POST', body: fd },
    )
  },

  extDataIngest: (id: string, body: { date?: string; rows: Record<string, unknown>[] }) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/ingest`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  extDataSchemaAll: () =>
    request<{ items: { id: string; label: string; mode: string; columns: { name: string; type: string; label: string }[] }[] }>('/api/ext-data/schema-all'),

  extDataPullConfig: (id: string, body: {
    url: string; method?: string; headers?: Record<string, string>; body?: string;
    response_path?: string; field_map?: Record<string, string>;
    schedule_minutes?: number; enabled?: boolean;
  }) =>
    request<{ status: string; pull: PullConfig }>(
      `/api/ext-data/${id}/pull`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  extDataPullTest: (id: string) =>
    request<{ status: string; total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean }>(
      `/api/ext-data/${id}/pull/test`,
      { method: 'POST' },
    ),

  extDataPullRun: (id: string) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/pull/run`,
      { method: 'POST' },
    ),

  // 内置预设 (概念/行业) 手动获取数据: 走结构转换, 保证 schema 一致
  extDataPresetFetch: (id: string) =>
    request<{ status: string; rows: number }>(
      `/api/ext-data/presets/${id}/fetch`,
      { method: 'POST' },
    ),

  extDataDetectFields: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ fields: { name: string; dtype: string; label: string }[]; rows: number; symbol_candidates: string[]; code_candidates: string[] }>(
      '/api/ext-data/detect-fields',
      { method: 'POST', body: fd },
    )
  },

  extDataFixSymbol: (id: string) =>
    request<{ status: string; fixed_files: number }>(
      `/api/ext-data/${id}/fix-symbol`,
      { method: 'POST' },
    ),

  // ===== Financials =====
  financialStatus: () =>
    request<FinancialStatus>('/api/financials/status'),

  financialMetrics: (symbol?: string) =>
    request<{ data: FinancialMetricRecord[] }>(
      `/api/financials/metrics${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialIncome: (symbol?: string) =>
    request<{ data: FinancialIncomeRecord[] }>(
      `/api/financials/income${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialBalanceSheet: (symbol?: string) =>
    request<{ data: FinancialBalanceSheetRecord[] }>(
      `/api/financials/balance-sheet${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialCashFlow: (symbol?: string) =>
    request<{ data: FinancialCashFlowRecord[] }>(
      `/api/financials/cash-flow${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  /** 触发财务数据同步(后台异步执行,接口立即返回 started 状态) */
  financialSync: (table: string) =>
    request<{ status: string; synced: { started: boolean; reason?: string } }>(
      `/api/financials/sync/${table}`, { method: 'POST' },
    ),

  /** AI 分析报告 CRUD */
  financialReportsList: () =>
    request<{ reports: AiFinancialReport[] }>('/api/financials/reports'),

  financialReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    periods?: number; summary?: string
  }) =>
    request<{ ok: boolean; report: AiFinancialReport }>('/api/financials/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  financialReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/financials/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 财务分析 — 流式调用。
   *
   * 返回一个可逐行读取的 async generator,每行是 JSON:
   *   {type:"meta",symbol,summary,periods}
   *   {type:"delta",content:"..."}    ← 文本片段,逐个累加
   *   {type:"error",message:"..."}
   *   {type:"done"}
   *
   * 用 ReadableStream 解析(而非 SSE EventSource),支持 POST body 且更简单。
   */
  async *financialAnalyzeStream(symbol: string, focus?: string, profileId?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    symbol?: string
    summary?: string
    periods?: number
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/financials/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '', ...(profileId ? { profile_id: profileId } : {}) }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // 按行分割(保留最后不完整的行在 buf)
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try {
          yield JSON.parse(s)
        } catch {
          // 忽略无法解析的行
        }
      }
    }
    // 处理残余
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== 个股分析 =====
  stockAnalysisLevels: (symbol: string, days = 120) =>
    request<StockLevels>(`/api/stock-analysis/levels?symbol=${encodeURIComponent(symbol)}&days=${days}`),

  stockAnalysisReportsList: () =>
    request<{ reports: AiStockReport[] }>('/api/stock-analysis/reports'),

  stockAnalysisReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    summary?: string; close?: number | null
    levels?: Record<LevelType, PriceLevel[]>
  }) =>
    request<{ ok: boolean; report: AiStockReport }>('/api/stock-analysis/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  stockAnalysisReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/stock-analysis/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 个股四维分析 — 流式调用(NDJSON,与财务分析同协议)。
   * meta 里额外带 levels(关键价位)供图表回放。
   */
  async *stockAnalyzeStream(symbol: string, focus?: string, profileId?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    symbol?: string
    summary?: string
    levels?: Record<LevelType, PriceLevel[]>
    close?: number | null
    content?: string
    message?: string
    /** P3: meta chunk 可带执行元信息;流式 provider 不上报 usage 时缺失,不得展示伪数据 */
    ai_meta?: AiExecutionMeta | null
  }> {
    const res = await fetch('/api/stock-analysis/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '', ...(profileId ? { profile_id: profileId } : {}) }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== 复盘数据分区(DuckDB enriched 面板聚合,按 Tab 懒加载) =====
  reviewEmotion: (asOf?: string, days = 30) =>
    request<ReviewEmotion>(`/api/review/emotion?days=${days}${asOf ? `&as_of=${asOf}` : ''}`),

  reviewLadder: (asOf?: string, days = 20) =>
    request<ReviewLadder>(`/api/review/ladder?days=${days}${asOf ? `&as_of=${asOf}` : ''}`),

  reviewRotation: (asOf?: string, days = 10, top = 8) =>
    request<ReviewRotation>(`/api/review/rotation?days=${days}&top=${top}${asOf ? `&as_of=${asOf}` : ''}`),

  reviewClues: (asOf?: string, limit = 20) =>
    request<ReviewClues>(`/api/review/clues?limit=${limit}${asOf ? `&as_of=${asOf}` : ''}`),

  reviewHkBreadth: (asOf?: string, days = 30) =>
    request<HkBreadth>(`/api/review/hk/breadth?days=${days}${asOf ? `&as_of=${asOf}` : ''}`),

  reviewHkMovers: (asOf?: string, limit = 20) =>
    request<HkMovers>(`/api/review/hk/movers?limit=${limit}${asOf ? `&as_of=${asOf}` : ''}`),

  // ===== 大盘复盘 =====
  reviewReportsList: () =>
    request<{ reports: AiReviewReport[] }>('/api/market-recap/reports'),

  reviewReportSave: (r: {
    as_of: string; focus?: string; content: string
    summary?: string; emotion_score?: number | null; emotion_label?: string
  }) =>
    request<{ ok: boolean; report: AiReviewReport }>('/api/market-recap/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  reviewReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/market-recap/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 大盘复盘 — 流式调用(NDJSON,与个股/财务分析同协议)。
   * meta 里带 as_of / emotion_score / emotion_label / summary,供前端先渲染信号灯。
   */
  async *reviewStream(asOf?: string, focus?: string, profileId?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    as_of?: string
    emotion_score?: number
    emotion_label?: string
    summary?: string
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/market-recap/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ as_of: asOf ?? null, focus: focus ?? '', ...(profileId ? { profile_id: profileId } : {}) }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== Strategy Engine =====
  strategyList: () =>
    request<{ strategies: StrategyDetail[] }>('/api/strategies'),

  strategyGet: (id: string) =>
    request<StrategyDetail>(`/api/strategies/${id}`),

  strategyRun: (strategyId: string, params?: Record<string, any>, asOf?: string, pool?: string[]) =>
    request<ScreenerResult>('/api/strategies/run', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, params, as_of: asOf ?? null, pool }),
    }),

  strategyRunAll: (asOf?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string }> }>(
      '/api/strategies/run-all',
      { method: 'POST', body: JSON.stringify({ as_of: asOf ?? null }) },
    ),

  strategySaveConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyResetConfig: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/config/${strategyId}`, { method: 'DELETE' }),

  /** 删除自定义策略（内置策略不可删除） */
  strategyDelete: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/${strategyId}`, { method: 'DELETE' }),

  strategyReload: () =>
    request<{ ok: boolean; count: number }>('/api/strategies/reload', { method: 'POST' }),

  // ===== Custom Signals (自定义信号) =====
  customSignalsList: () =>
    request<{ signals: CustomSignal[] }>('/api/custom-signals'),

  customSignalsOptions: () =>
    request<CustomSignalOptions>('/api/custom-signals/options'),

  customSignalSave: (signal: CustomSignal) =>
    request<{ ok: boolean; signal: CustomSignal }>('/api/custom-signals', {
      method: 'POST',
      body: JSON.stringify(signal),
    }),

  customSignalDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/custom-signals/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // ===== Monitor Rules (监控规则) =====
  monitorRulesList: () =>
    request<{ rules: MonitorRule[] }>('/api/monitor-rules'),

  monitorRuleOptions: () =>
    request<MonitorRuleOptions>('/api/monitor-rules/options'),

  monitorRuleSave: (rule: MonitorRule) =>
    request<{ ok: boolean; rule: MonitorRule }>('/api/monitor-rules', {
      method: 'POST',
      body: JSON.stringify(rule),
    }),

  monitorRuleDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/monitor-rules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 生成演示监控规则 (Dev 页用) */
  monitorRuleSeed: () =>
    request<{ ok: boolean; generated: number }>('/api/monitor-rules/seed', { method: 'POST' }),

  // ===== Alerts (触发记录) =====
  alertsList: (params?: { days?: number; limit?: number; source?: string; type?: string }) => {
    const qs = new URLSearchParams()
    if (params?.days) qs.set('days', String(params.days))
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.source) qs.set('source', params.source)
    if (params?.type) qs.set('type', params.type)
    const s = qs.toString()
    return request<{ alerts: AlertEvent[]; total: number }>(`/api/alerts${s ? `?${s}` : ''}`)
  },

  alertsClear: () =>
    request<{ ok: boolean; cleared: number }>('/api/alerts', { method: 'DELETE' }),

  alertDelete: (ts: number) =>
    request<{ ok: boolean }>(`/api/alerts/${ts}`, { method: 'DELETE' }),

  /** 生成演示触发记录 (Dev 页用) */
  alertSeed: (count = 12, recent = true) =>
    request<{ ok: boolean; generated: number }>(`/api/alerts/seed?count=${count}&recent=${recent}`, { method: 'POST' }),

  /** 检查 AI 配置状态 */
  strategyAiStatus: () =>
    request<{ configured: boolean; has_key: boolean; has_model: boolean; provider?: string }>('/api/strategies/ai/status'),

  /** 测试 AI 连通性 */
  strategyAiTest: () =>
    request<{ ok: boolean; error?: string; model?: string; response?: string; usage?: { prompt: number; completion: number } }>(
      '/api/strategies/ai/test',
      { method: 'POST' },
    ),

  /** 获取策略源文件内容 */
  strategyGetSource: (id: string) =>
    request<{ code: string; source: string }>(`/api/strategies/${id}/source`),
  strategyBuild: (step: number, payload: Record<string, any>, profileId?: string) =>
    request<{ code: string; meta: Record<string, any>; valid: boolean; error: string | null }>(
      '/api/strategies/build',
      { method: 'POST', body: JSON.stringify({ step, ...payload, ...(profileId ? { profile_id: profileId } : {}) }) },
    ),

  /** 保存 AI 生成的策略文件 */
  strategySaveCode: (strategyId: string, code: string) =>
    request<{ ok: boolean; path: string }>('/api/strategies/ai/save', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, code }),
    }),
}

// ===== Pipeline =====
export interface PipelineJob {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'degraded' | 'failed'
  stage: string
  progress: number          // 0-100 整体进度
  stage_pct: number         // 0-100 当前阶段内进度
  log: { ts: string; stage: string; msg: string }[]
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  result: {
    universe_size: number
    daily_days: number
    adj_factor_symbols: number
    enriched_days: number
    index_count?: number
    index_daily_rows?: number
    minute_rows: number
    skipped_stages?: string[]
    failed_stages?: { stage: string; error: string }[]
  } | null
  error: string | null
}

export type PipelineJobSummary = Omit<PipelineJob, 'log'>

// ===== Data status =====
interface TableStats {
  rows: number
  earliest_date: string | null
  latest_date: string | null
  symbols_covered: number
  trading_days: number
}

interface InstrumentsStats {
  rows: number
  symbols_covered: number
  latest_as_of: string | null
  named: number
}

export interface DataStatus {
  daily: TableStats | null
  enriched: TableStats | null
  index_daily: TableStats | null
  index_enriched: TableStats | null
  index_instruments: InstrumentsStats | null
  etf_daily: TableStats | null
  etf_enriched: TableStats | null
  etf_instruments: InstrumentsStats | null
  minute: TableStats | null
  adj_factor: TableStats | null
  instruments: InstrumentsStats | null
  financials: { rows: number; tables: Record<string, { rows: number; symbols: number }> } | null
  storage: {
    daily_files: number
    daily_size_mb: number
    enriched_files: number
    enriched_size_mb: number
    index_daily_files?: number
    index_daily_size_mb?: number
    index_enriched_files?: number
    index_enriched_size_mb?: number
    index_instruments_files?: number
    index_instruments_size_mb?: number
    etf_daily_files?: number
    etf_daily_size_mb?: number
    etf_enriched_files?: number
    etf_enriched_size_mb?: number
    etf_instruments_files?: number
    etf_instruments_size_mb?: number
    etf_adj_factor_files?: number
    etf_adj_factor_size_mb?: number
    minute_files: number
    minute_size_mb: number
    adj_factor_files: number
    adj_factor_size_mb: number
    instruments_files: number
    instruments_size_mb: number
    financials_files?: number
    financials_size_mb?: number
    ext_data_files?: number
    ext_data_size_mb?: number
    total_size_mb: number
  }
  next_pipeline_run: string | null
  next_instruments_run: string | null
  last_pipeline_run: string | null
  last_instruments_run: string | null
  checked_at: string
}

export interface EnrichedField {
  name: string
  type: string
  desc: string
}

// ===== 扩展数据 =====
export interface ExtDataField {
  name: string
  dtype: string
  label: string
}

export interface PullConfig {
  url: string
  method: string
  headers?: Record<string, string>
  body?: string | null
  response_path: string
  field_map?: Record<string, string>
  schedule_minutes: number
  enabled: boolean
  last_run?: string | null
  last_status?: string | null
  last_message?: string | null
  last_rows?: number | null
}

export interface ExtDataConfig {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  fields: ExtDataField[]
  description?: string
  symbol_map?: Record<string, string>
  code_map?: Record<string, string>
  created_at: string
  updated_at: string
  latest_sync_date?: string | null
  date_range?: string[] | null
  pull?: PullConfig | null
}

export interface ExtDataRowsResult {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  date: string | null
  total: number
  limit: number
  fields: ExtDataField[]
  rows: Record<string, any>[]
}

export interface AnalysisColumn {
  field: string
  label?: string
  type?: 'string' | 'number' | 'percent' | 'amount' | 'date'
  width?: number | null
  sortable?: boolean
  precision?: number | null
  format?: string | null
  aggregate?: 'count' | 'avg' | 'sum' | 'min' | 'max' | null
  visible?: boolean
}

export interface AnalysisMenu {
  id: string
  label: string
  icon: string
  data_source: string
  template: 'dimension_rank' | 'ranking' | 'table'
  dimension_field?: string | null
  rank_field?: string | null
  group_columns: AnalysisColumn[]
  detail_columns: AnalysisColumn[]
  default_sort?: { field: string; order: 'asc' | 'desc' } | null
  visible: boolean
  order: number
  created_at?: string | null
  updated_at?: string | null
  builtin?: boolean
}

// ===== Trading (YMOS 交易域) =====
// 后端: api/trading.py + api/trading_review.py + api/trading_plans.py + api/strategy_profile.py
// 契约以 services/trading/*.py 源码为准: 事件流 append-only, 单笔文件是当前事实的缓存投影。

export type TradeStatus = '计划中' | '持仓中' | '已平仓'

export type TradeEventKind =
  | 'open' | 'prepare' | 'revise' | 'fill'
  | 'add' | 'tp' | 'sl' | 'adjust' | 'close'

export interface TradeThesis {
  text: string
  invalidation: string
  createdAt: string
}

export interface TradePlanLeg {
  qty: number | null
  price: number | null
  ts: string
}

/** 单笔交易当前事实 (data/user_data/trading/trades/{id}.json) */
export interface Trade {
  schemaVersion: number
  tradeId: string
  symbol: string
  name: string
  status: TradeStatus
  strategy: string | null
  thesis: TradeThesis
  stopLoss: number | null
  exitRule?: string
  position: { qty: number; costPrice: number; invested: number }
  realizedPnl: number
  createdAt: string
  closedAt: string | null
  /** prepare 事件写入的建仓计划 */
  plan?: TradePlanLeg
  /** revise 事件累积的修订历史 */
  planRevisions?: TradePlanLeg[]
}

/** 生命周期事件 (trade_events.jsonl, 只追加) */
export interface TradeEvent {
  schemaVersion: number
  tradeId: string
  kind: TradeEventKind
  ts: string
  payload: Record<string, unknown>
  note: string
  /** 门禁未通过但用户确认执行 → 绕门留痕 */
  gateBypassed?: boolean
  /** 计划偏差接口补充的标的 */
  symbol?: string
}

export interface GateCheckResult {
  id: string
  name: string
  passed: boolean
  detail: string
}

export interface GateEvaluation {
  passed: boolean
  gates: GateCheckResult[]
  missing: string[]
}

/** 决策审计条目 (decision_audit.jsonl, 拦截/放行均留痕) */
export interface AuditEntry {
  schemaVersion: number
  ts: string
  mode: string
  tradeId: string
  symbol: string
  passed: boolean
  gates: GateCheckResult[]
  missing: string[]
  note: string
}

export interface AccountChange {
  ts: string
  amount: number
  reason: string
}

export interface TradingAccount {
  id: string
  currency: string
  capital: number
  horizonFundMonths: number
  maxSingleRatio: number
  changes: AccountChange[]
}

export interface AccountsDoc {
  schemaVersion: number
  accounts: TradingAccount[]
}

export type PortfolioHealth = 'normal' | 'attention' | 'critical'

export interface PortfolioPosition {
  tradeId: string
  symbol: string
  name: string
  qty: number
  costPrice: number
  price: number | null
  marketValue: number | null
  unrealizedPnl: number | null
  stopLoss: number | null
  stopLossDistance: number | null
  thesis: TradeThesis
  stale: boolean
  exposure?: number | null
}

export interface FholdAccount {
  id: string
  name: string
  broker: string
  isDefault: boolean
}

export interface FholdPosition {
  symbol: string | null
  code: string
  name: string
  accountId: string
  qty: number
  costPrice: number
  currentPrice: number
  marketValue: number
  holdingPnl: number
  holdingPnlRatio: number
  sourceDate?: string
  updatedAt?: string
}

export interface FholdHoldings {
  available: boolean
  accounts: FholdAccount[]
  positions: FholdPosition[]
}

/** 组合快照 (GET /api/trading/portfolio, 实时计算派生值) */
export interface PortfolioSnapshot {
  nav: number
  capital: number
  realizedPnl: number
  unrealizedPnl: number
  positionsValue: number
  available: number
  pendingPlansAmount: number
  positions: PortfolioPosition[]
  health: PortfolioHealth
  stale: boolean
  priceSource: string
  maxSingleRatio: number
  fhold: FholdHoldings
}

/** 机械红旗 (red_flags.py):放宽止损/亏损加仓/绕过门禁/审计断链 + P6 期限超限/仓位超限/门禁膨胀 */
export interface RedFlag {
  type:
    | 'stop_loss_widened'
    | 'loss_add'
    | 'gate_bypassed'
    | 'audit_missing'
    | 'horizon_exceeded'   // 持仓超限(单笔级)
    | 'size_over_limit'    // 仓位超限(单笔级)
    | 'gate_proliferation' // 门禁膨胀(全局级)
    | string
  ts: string
  kind?: string
  old?: number
  new?: number
  price?: number
  costPrice?: number
  /** P6 新类型:后端预格式化文案,直接展示 */
  detail?: string
  // horizon_exceeded
  holdingDays?: number
  horizonMonths?: number
  limitDays?: number
  // size_over_limit
  marketValue?: number
  nav?: number
  exposure?: number
  breached?: string[]
  maxSingleRatio?: number
  positionLimitPct?: number
  // gate_proliferation(全局级)
  ruleCount?: number
  threshold?: number
}

/** AI 归因结果 (autopsy.py: A 策略正常不利 / B 执行偏离 / C 规则歧义冲突 / D 数据问题) */
export interface AutopsyResult {
  schemaVersion: number
  tradeId: string
  classification: string
  reasoning: string
  fix: string
  rawResponse: string
  redFlags: RedFlag[]
  ts: string
  /** P3: 执行元信息(实际 profile/模型/fallback/usage),旧落盘记录可缺失 */
  ai_meta?: AiExecutionMeta | null
}

/** 盘后状态驱动归因结果 (POST /api/trading/review/auto-run) */
export interface AutoReviewResult {
  level: 'L0' | 'L1'
  candidates: number
  autopsied: number
  skipped: number
  errors?: Array<{ tradeId: string; error: string }>
  /** AI 未配置时为 'blocked_by_dependency' */
  code?: string
  detail?: string
}

export type ProposalStatus = 'draft' | 'approved' | 'rejected' | 'trial' | 'verified'

export interface ProposalHistoryItem {
  ts: string
  from: string
  to: string
  note: string
}

export interface Proposal {
  schemaVersion: number
  id: string
  title: string
  target: string
  evidence: unknown[]
  before: Record<string, unknown>
  after: Record<string, unknown>
  falsifier: string
  sampleSize: number
  status: ProposalStatus
  createdAt: string
  updatedAt: string
  history: ProposalHistoryItem[]
  /** P6:属放宽 && 近30天有亏损平仓 → true,审批警示 */
  relaxationAfterLoss?: boolean
}

export type GateRuleMode = 'buy_new' | 'add' | 'tp' | 'sl' | 'close' | 'adjust'

export interface GateRuleItem {
  id: string
  text: string
}

export interface GateRuleSection {
  all: GateRuleItem[]
  any: GateRuleItem[]
  discipline: GateRuleItem[]
}

export type GateRulesMap = Record<GateRuleMode, GateRuleSection>

export interface GateRulesDoc {
  schemaVersion: number
  rules: GateRulesMap
}

export type PlanAction = 'buy_new' | 'add' | 'tp' | 'sl' | 'close' | 'adjust' | 'watch'

export interface PlanEntry {
  id: string
  symbol: string
  tradeId: string | null
  action: PlanAction
  trigger: string
  qty: number | null
  reason: string
  createdAt: string
  /** P4 additive fields; old saved plans may omit them. */
  strategyId?: string | null
  plannedPrice?: number | null
  stopLoss?: number | null
  exitRule?: string
  thesisHorizonMonths?: number | null
  invalidation?: string
}

export interface TradePlanDoc {
  schemaVersion: number
  date: string
  entries: PlanEntry[]
  actualNotes: string
}

export interface PlanDeviationPlanned {
  key: string[]
  id: string
  symbol: string
  action: string
  tradeId: string | null
}

export interface PlanDeviationDone {
  key: string[]
  symbol: string
  kind: string
  tradeId: string
  ts: string
}

export interface PlanDeviation {
  date: string
  plannedCount: number
  doneCount: number
  planned_but_not_done: PlanDeviationPlanned[]
  done_but_not_planned: PlanDeviationDone[]
  matched: PlanDeviationPlanned[]
}

export interface AnalysisTraceNode {
  id: string
  kind: string
  label: string
  status: 'pass' | 'fail' | 'unknown' | 'skipped' | string
  source_refs: string[]
  reason?: string | null
  locked: boolean
  depends_on: string[]
}

export interface PlanCheckGate {
  status: 'proceed' | 'wait' | 'unknown'
  reasons: string[]
  missing_inputs: string[]
  data_as_of: string
  source: string
  program_rules_version: string
}

export interface PlanCheckStage1 {
  trend: string
  volatility: string
  liquidity: string
  readiness: 'sufficient' | 'insufficient'
  conflicts: string[]
  notes: string[]
}

export interface PlanCheckReview {
  checks: Array<{ item: string; conclusion: '满足' | '部分满足' | '不满足'; reason: string }>
  summary: string
}

export interface PlanCheckResult {
  status: 'no_action' | 'review_ready'
  gate: PlanCheckGate
  stage1: PlanCheckStage1 | null
  review: PlanCheckReview | null
  disclaimer: string
  ai_meta?: AiExecutionMeta | null
  warnings: string[]
}

export interface PlanCheckArtifact {
  id?: string
  attempt_id: string
  request_id: string
  purpose?: string
  status: 'ok' | 'failed' | 'cancelled'
  data_as_of?: string | null
  symbol?: string | null
  result: PlanCheckResult
  trace: AnalysisTraceNode[]
  usage: AiUsageMeta
  warnings: string[]
}

export type PlanCheckStreamEvent =
  | { type: 'meta'; attempt_id: string; request_id: string; date: string; entry_id: string }
  | { type: 'progress'; kind: string; stage?: string; status?: string; [key: string]: unknown }
  | ({ type: 'result' } & PlanCheckArtifact)
  | { type: 'error'; code: string; message: string }
  | { type: 'done'; attempt_id: string; request_id: string }

export interface PlanCheckSummary {
  id: string
  attempt_id: string
  request_id: string
  status: string
  symbol?: string | null
  market?: string | null
  profile_id?: string | null
  created_at?: string | null
  result_status?: string | null
}

export interface StrategyProfileInvalidation {
  name: string
  observable: string
  action: string
}

/** 策略坐标卡 family 合法值(P6.3) */
export type StrategyFamily =
  | 'value' | 'growth' | 'trend' | 'event'
  | 'short_horizon' | 'relative_value' | 'mixed'

/** family=mixed 时必填的四要素裁决 */
export interface StrategyFamilyMix {
  entryJudge: string
  invalidationAuthority: string
  sizingHorizon: string
  conflictResolution: string
}

/** 策略剧本(P6.3,三文本均可缺省) */
export interface StrategyPlaybook {
  scope?: string
  entry?: string
  exit?: string
}

export interface StrategyProfile {
  schemaVersion: number
  strategyId: string
  invalidation: StrategyProfileInvalidation[]
  risk: { positionLimitPct: number; lossBudgetPct: number; thesisHorizonMonths: number }
  cadence: { review: string }
  /** 策略坐标卡(P6.3 可选) */
  family?: StrategyFamily | string
  /** family=mixed 时必填 */
  familyMix?: StrategyFamilyMix
  /** 策略剧本(P6.3 可选) */
  playbook?: StrategyPlaybook
}

export interface StrategyProfileCheck {
  id: string
  name: string
  status: 'pass' | 'partial' | 'fail' | 'insufficient_evidence' | string
  detail: string
}

/** 策略体检响应(ai=true 时追加 AI 深度体检报告) */
export interface StrategyValidateResult {
  checks: StrategyProfileCheck[]
  aiReport?: string | null
  aiError?: string
  /** P3: AI 深度体检的执行元信息(实际 profile/模型/fallback/usage),旧响应可缺失 */
  ai_meta?: AiExecutionMeta | null
}

// ── 生命周期 (api/trading.py) ──

export function tradingListTrades(status?: TradeStatus | string) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return request<{ trades: Trade[] }>(`/api/trading/trades${qs}`)
}

export function tradingGetTrade(id: string) {
  return request<{ trade: Trade; events: TradeEvent[] }>(
    `/api/trading/trades/${encodeURIComponent(id)}`,
  )
}

export interface TradingOpenPayload {
  symbol: string
  name: string
  thesis: { text: string; invalidation: string }
  stopLoss?: number | null
  strategy?: string
  note?: string
  gate?: { confirmed?: boolean }
}

export function tradingOpenTrade(payload: TradingOpenPayload) {
  return request<Trade>('/api/trading/trades', { method: 'POST', body: JSON.stringify(payload) })
}

export interface TradingAppendEventPayload {
  kind: Exclude<TradeEventKind, 'open'>
  payload?: Record<string, unknown>
  note?: string
  /** 门禁预检未过时由用户确认后带上: {confirmed: true} → 绕门留痕 */
  gate?: { confirmed?: boolean; gates?: GateCheckResult[]; missing?: string[] }
}

export function tradingAppendEvent(id: string, payload: TradingAppendEventPayload) {
  return request<Trade>(`/api/trading/trades/${encodeURIComponent(id)}/events`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function tradingListAudit(params?: { tradeId?: string; passed?: boolean; limit?: number }) {
  const sp = new URLSearchParams()
  if (params?.tradeId) sp.set('trade_id', params.tradeId)
  if (params?.passed !== undefined) sp.set('passed', String(params.passed))
  if (params?.limit) sp.set('limit', String(params.limit))
  const qs = sp.toString()
  return request<{ audit: AuditEntry[] }>(`/api/trading/audit${qs ? `?${qs}` : ''}`)
}

// ── 账户 / 组合快照 ──

export function tradingGetAccounts() {
  return request<AccountsDoc>('/api/trading/accounts')
}

export function tradingPutAccounts(payload: { accounts: TradingAccount[] }) {
  return request<AccountsDoc>('/api/trading/accounts', {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function tradingGetPortfolio() {
  return request<PortfolioSnapshot>('/api/trading/portfolio')
}

// ── 红旗 / AI 归因 (api/trading_review.py) ──

export function tradingGetRedFlags() {
  return request<{ flags: Record<string, RedFlag[]> }>('/api/trading/red-flags')
}

export function tradingGetTradeRedFlags(id: string) {
  return request<{ tradeId: string; flags: RedFlag[] }>(
    `/api/trading/trades/${encodeURIComponent(id)}/red-flags`,
  )
}

export function tradingRunAutopsy(id: string, profileId?: string) {
  const qs = profileId ? `?${new URLSearchParams({ profile_id: profileId })}` : ''
  return request<AutopsyResult>(`/api/trading/trades/${encodeURIComponent(id)}/autopsy${qs}`, {
    method: 'POST',
  })
}

export function tradingGetAutopsy(id: string) {
  // 404 = 尚未生成归因(正常状态), 静默返回 null 不弹 toast
  return request<AutopsyResult | null>(
    `/api/trading/trades/${encodeURIComponent(id)}/autopsy`,
    undefined,
    { silent404: true },
  )
}

/** 盘后状态驱动 AI 归因(L0/L1):POST /api/trading/review/auto-run */
export function tradingRunAutoReview() {
  return request<AutoReviewResult>('/api/trading/review/auto-run', { method: 'POST' })
}

// ── 策略变更提案 ──

export function tradingListProposals(status?: ProposalStatus | string) {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return request<{ proposals: Proposal[] }>(`/api/trading/proposals${qs}`)
}

export function tradingCreateProposal(payload: Partial<Proposal> & { falsifier: string }) {
  return request<Proposal>('/api/trading/proposals', { method: 'POST', body: JSON.stringify(payload) })
}

export function tradingUpdateProposal(id: string, payload: Record<string, unknown>) {
  return request<Proposal>(`/api/trading/proposals/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

// ── 门禁 (api/trading_plans.py) ──

export function tradingGetGateRules() {
  return request<GateRulesDoc>('/api/trading/gate-rules')
}

export function tradingPutGateRules(payload: { rules: Partial<GateRulesMap> }) {
  return request<GateRulesDoc>('/api/trading/gate-rules', {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function tradingEvaluateGates(payload: {
  mode: string
  tradeId?: string
  payload: Record<string, unknown>
}) {
  return request<GateEvaluation>('/api/trading/gates/evaluate', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

// ── 交易计划台 ──

export function tradingGetPlan(date: string) {
  return request<TradePlanDoc>(`/api/trading/plans/${date}`)
}

export function tradingPutPlan(
  date: string,
  payload: { entries: PlanEntry[]; actualNotes?: string; replace?: boolean; schemaVersion?: number },
) {
  return request<TradePlanDoc>(`/api/trading/plans/${date}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function tradingGetPlanDeviation(date: string) {
  return request<PlanDeviation>(`/api/trading/plans/${date}/deviation`)
}

export async function* tradingCheckPlanStream(
  date: string,
  entryId: string,
  profileId?: string,
  signal?: AbortSignal,
): AsyncGenerator<PlanCheckStreamEvent> {
  const qs = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : ''
  const res = await fetch(
    `/api/trading/plans/${encodeURIComponent(date)}/entries/${encodeURIComponent(entryId)}/check${qs}`,
    { method: 'POST', signal },
  )
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = JSON.parse(await res.text()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch { /* retain status */ }
    throw new Error(detail)
  }
  if (!res.body) throw new Error('计划检查响应无流数据')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.trim()) continue
      yield JSON.parse(line) as PlanCheckStreamEvent
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer) as PlanCheckStreamEvent
}

export function tradingListPlanChecks(symbol?: string, limit = 50) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (symbol) params.set('symbol', symbol)
  return request<{ items: PlanCheckSummary[] }>(`/api/trading/plan-checks?${params}`)
}

export function tradingGetPlanCheck(attemptId: string) {
  return request<PlanCheckArtifact>(`/api/trading/plan-checks/${encodeURIComponent(attemptId)}`)
}

export function tradingPlanCheckExportUrl(attemptId: string, format: 'json' | 'markdown') {
  return `/api/trading/plan-checks/${encodeURIComponent(attemptId)}/export?format=${format}`
}

// ── 策略风险声明 (api/strategy_profile.py, prefix=/api/strategies) ──

export function strategyGetProfile(id: string) {
  return request<{ profile: StrategyProfile }>(
    `/api/strategies/${encodeURIComponent(id)}/profile`,
  )
}

export function strategyPutProfile(
  id: string,
  payload: Omit<StrategyProfile, 'schemaVersion' | 'strategyId'> | Record<string, unknown>,
) {
  return request<{ ok: boolean; profile: StrategyProfile }>(
    `/api/strategies/${encodeURIComponent(id)}/profile`,
    { method: 'PUT', body: JSON.stringify(payload) },
  )
}

export function strategyDeleteProfile(id: string) {
  return request<{ ok: boolean }>(`/api/strategies/${encodeURIComponent(id)}/profile`, {
    method: 'DELETE',
  })
}

export function strategyValidateProfile(id: string, ai = false, profileId?: string) {
  const params = new URLSearchParams()
  if (ai) params.set('ai', 'true')
  // P3: additive — 选中 profile 时传给后端路由;缺省走后端默认/路由策略
  if (profileId) params.set('profile_id', profileId)
  const qs = params.toString()
  return request<StrategyValidateResult>(
    `/api/strategies/${encodeURIComponent(id)}/profile/validate${qs ? `?${qs}` : ''}`,
  )
}
