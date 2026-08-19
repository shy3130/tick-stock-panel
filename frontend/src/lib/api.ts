// 后端 API 客户端 — 全项目统一入口
//
// Dev:Vite 代理 /api 到 :3018
// Prod:同源(FastAPI 托管前端 dist)

import { toast } from '@/components/Toast'
import type { ExperimentRuntime } from './runStatus'

const BASE = ''

async function request<T>(path: string, init?: RequestInit, opts?: { silent404?: boolean; acceptUnavailable?: boolean }): Promise<T> {
  const isFormData = init?.body instanceof FormData
  const headers: Record<string, string> = {}
  if (!isFormData) headers['Content-Type'] = 'application/json'
  const res = await fetch(`${BASE}${path}`, { ...init, headers })
  if (!res.ok) {
    // 404 对调用方是"无数据"语义(如尚未生成 AI 归因)时静默返回 null,不弹 toast
    if (opts?.silent404 && res.status === 404) return null as T
    if (opts?.acceptUnavailable && res.status === 503) return res.json() as Promise<T>
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

export type InstrumentAssetType = 'stock' | 'index' | 'etf' | 'hk'

export interface InstrumentSearchResult {
  symbol: string
  name: string
  code: string
  asset_type?: InstrumentAssetType | 'unknown'
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
  /** 报告期 (canonical; raw 列 t_date 回填) */
  period_end: string
  /** 公告日期 (canonical; raw 列 notice_date 回填, 0001-01-01 哨兵置空) */
  announce_date?: string | null
  /** raw 报告期 (fstore t_date, 透传保留) */
  t_date?: string | null
  /** raw 公告日 (fstore notice_date, 透传保留) */
  notice_date?: string | null
  /** 数据来源 provenance (provider:channel:table) */
  source?: string | null
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
  [key: string]: unknown
}

export interface FinancialIncomeRecord {
  symbol?: string
  /** 报告期 (canonical; raw 列 t_date 回填) */
  period_end: string
  /** 公告日期 (canonical; raw 列 notice_date 回填, 0001-01-01 哨兵置空) */
  announce_date?: string | null
  /** raw 报告期 (fstore t_date, 透传保留) */
  t_date?: string | null
  /** raw 公告日 (fstore notice_date, 透传保留) */
  notice_date?: string | null
  /** 数据来源 provenance (provider:channel:table) */
  source?: string | null
  revenue?: number | null
  operating_cost?: number | null
  operating_profit?: number | null
  total_profit?: number | null
  net_income?: number | null
  net_income_attributable?: number | null
  basic_eps?: number | null
  diluted_eps?: number | null
  [key: string]: unknown
}

export interface FinancialBalanceSheetRecord {
  symbol?: string
  /** 报告期 (canonical; raw 列 t_date 回填) */
  period_end: string
  /** 公告日期 (canonical; raw 列 notice_date 回填, 0001-01-01 哨兵置空) */
  announce_date?: string | null
  /** raw 报告期 (fstore t_date, 透传保留) */
  t_date?: string | null
  /** raw 公告日 (fstore notice_date, 透传保留) */
  notice_date?: string | null
  /** 数据来源 provenance (provider:channel:table) */
  source?: string | null
  total_assets?: number | null
  total_current_assets?: number | null
  cash_and_equivalents?: number | null
  total_liabilities?: number | null
  total_equity?: number | null
  equity_attributable?: number | null
  [key: string]: unknown
}

export interface FinancialCashFlowRecord {
  symbol?: string
  /** 报告期 (canonical; raw 列 t_date 回填) */
  period_end: string
  /** 公告日期 (canonical; raw 列 notice_date 回填, 0001-01-01 哨兵置空) */
  announce_date?: string | null
  /** raw 报告期 (fstore t_date, 透传保留) */
  t_date?: string | null
  /** raw 公告日 (fstore notice_date, 透传保留) */
  notice_date?: string | null
  /** 数据来源 provenance (provider:channel:table) */
  source?: string | null
  net_operating_cash_flow?: number | null
  net_investing_cash_flow?: number | null
  net_financing_cash_flow?: number | null
  capex?: number | null
  net_cash_change?: number | null
  [key: string]: unknown
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
  date?: string | null
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
  // 受控外部 fallback provenance — 仅外部降级数据出现; 本地/日线兜底不带
  source?: string
  degraded?: boolean
  [key: string]: any
}

// ===== 受控外部行情降级 (external fallback, 默认关闭) =====
// 契约: backend/docs/CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md §4.3
// 外部数据仅供展示, 绝不写入本地行情库, 不参与选股/监控/回测。

/** 外部降级行情来源标记 — 行级 source 命中即视为降级 */
export const EXTERNAL_QUOTE_SOURCE = 'tencent_quote'

/** 响应级 source 中表示外部 fallback 的 legacy 值(其余 realtime/provider_realtime/index_daily 均为本地数据, 不可误标) */
export const EXTERNAL_FALLBACK_RESPONSE_SOURCE = 'fallback_external'

export type IndexFallbackReason = 'local_snapshot_missing' | 'local_snapshot_stale'

/**
 * GET /api/intraday/indices 响应。
 * degraded / sources / fallback_reason 仅在实际外部 fallback 时出现;
 * 旧后端可整体缺失, 前端按未降级处理(完全向后兼容)。
 */
export interface IndexQuotesResponse {
  rows: IndexQuote[]
  count: number
  source?: string
  degraded?: boolean
  sources?: { realtime?: string }
  fallback_reason?: IndexFallbackReason
}

/** 是否处于外部源降级 — 响应级 degraded/source 或任一行 source=tencent_quote; 本地与日线兜底均不命中 */
export function indexQuotesDegraded(resp: IndexQuotesResponse | null | undefined): boolean {
  if (!resp) return false
  if (resp.degraded === true) return true
  if (resp.source === EXTERNAL_FALLBACK_RESPONSE_SOURCE) return true
  return (resp.rows ?? []).some(r => r?.source === EXTERNAL_QUOTE_SOURCE || r?.degraded === true)
}

export function indexFallbackReasonText(reason: IndexFallbackReason | string | null | undefined): string | null {
  if (reason === 'local_snapshot_missing') return '本地快照缺失'
  if (reason === 'local_snapshot_stale') return '本地快照已过期'
  return null
}

// ===== 自选/通用实时快照 (GET /api/intraday/snapshot) =====
// 与 indices 同形响应；只读展示，绝不写回 canonical/enriched/monitor/backtest。
// 行 change_pct 为【百分点】(5.0 = 5%)；Watchlist 的 rt_pct/change_pct 为小数比率，合并时必须 /100。

export interface IntradaySnapshotRow {
  symbol: string
  name?: string | null
  last_price?: number | null
  prev_close?: number | null
  /** 百分点 (5.0 = 5%)，非小数比率 */
  change_pct?: number | null
  amount?: number | null
  timestamp?: string | number | null
  // 受控外部 fallback provenance — 仅外部降级出现; 本地 realtime 不带
  source?: string
  degraded?: boolean
}

/**
 * GET /api/intraday/snapshot 响应。
 * symbols 最多 60 个；degraded / sources / fallback_reason 仅实际外部 fallback 时出现。
 * 本地当日快照不带降级标记。
 */
export interface IntradaySnapshotResponse {
  rows: IntradaySnapshotRow[]
  count: number
  source?: string
  degraded?: boolean
  sources?: { realtime?: string }
  fallback_reason?: IndexFallbackReason
}

/** 是否处于外部源降级 — 与 indexQuotesDegraded 同口径；本地 realtime/provider 不命中 */
export function intradaySnapshotDegraded(resp: IntradaySnapshotResponse | null | undefined): boolean {
  if (!resp) return false
  if (resp.degraded === true) return true
  if (resp.source === EXTERNAL_FALLBACK_RESPONSE_SOURCE) return true
  return (resp.rows ?? []).some(r => r?.source === EXTERNAL_QUOTE_SOURCE || r?.degraded === true)
}

/** 百分点 → 小数比率；非有限数返回 null（调用方不覆盖原值） */
export function snapshotPctToRatio(pctPoints: number | null | undefined): number | null {
  if (pctPoints == null || !Number.isFinite(Number(pctPoints))) return null
  return Number(pctPoints) / 100
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
  source: 'builtin' | 'custom' | 'ai' | 'composite'
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
  execution_backend?: 'polars_expr' | 'composite'
  composite_children?: Array<{
    id: string
    name?: string
    description?: string
    weight: number
  }> | null
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
  /**
   * 外部 depth 展示降级: 权威 sealed map 缺失且命中 get_display_depth_map 时为 true。
   * 仅连板页当前展示; 不修正 counts/status, 不写入 sealed/历史, 不参与选股回测监控。
   */
  sealed_degraded?: boolean
  /** 外部展示来源, 如 tencent_quote; 无外部 map 时为 null/缺省 */
  sealed_source?: string | null
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

export interface BacktestMetricContext {
  version: string
  return_frequency: 'daily' | 'weekly' | 'monthly' | 'custom'
  periods_per_year: number
  risk_free_rate?: number
  risk_free_rate_per_period?: number
  std_ddof: number
}

export interface BacktestDataSnapshot {
  canonical_generation?: string | null
  canonical_start_date?: string | null
  canonical_end_date?: string | null
  local_overlay_latest_date?: string | null
  data_start: string
  data_cutoff: string
  adjustment_mode: string
  adjustment_generation?: string | null
  source_generations?: Record<string, string>
  universe_definition?: Record<string, unknown>
  universe_as_of?: string | null
  snapshot_hash: string
}

export interface GroupStat {
  group: number
  label: string
  total_return: number
  annual_return: number | null
  max_drawdown: number
  sharpe: number | null
  win_rate: number
  /** 平均单期换手（标准单边口径）。追加字段：旧持久化结果可能缺失 */
  avg_turnover?: number
  /** 总换手（标准单边口径）。追加字段：旧持久化结果可能缺失 */
  total_turnover?: number
  /** 全期间交易成本合计（占期初净值比例）。追加字段：旧持久化结果可能缺失 */
  total_cost?: number
}

/** 多空组合统计。追加字段：旧持久化结果可能缺失 */
export interface LongShortStats {
  total_return?: number
  max_drawdown?: number
  top_group?: string
  bottom_group?: string
  avg_turnover?: number
  total_turnover?: number
  total_cost?: number
  annual_return?: number | null
  sharpe?: number | null
  annual_volatility?: number | null
  calmar?: number | null
  metric_context?: BacktestMetricContext
  sortino?: number | null
  omega?: number | null
  tail_ratio?: number | null
  ulcer_index?: number | null
  value_at_risk?: number | null
  conditional_value_at_risk?: number | null
  downside_deviation?: number | null
}

/** 分层单期换手序列数据点：date + 各分组单期换手（标准单边口径） */
export interface GroupTurnoverPoint {
  date: string
  [group: string]: number | string
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
  /** 分组单期换手序列。追加字段：旧持久化结果可能缺失 */
  group_turnover?: GroupTurnoverPoint[]
  long_short_stats: LongShortStats
  long_short_nav: { date: string; value: number }[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
  methodology_context?: string
  warnings?: string[]
  data_snapshot?: BacktestDataSnapshot
  metric_context?: BacktestMetricContext
  engine_version?: string
  random_seed?: number | null
  /** 后端是否已将完整结果保存为 BacktestRun；false 时不能下载 Run 报告。 */
  persisted?: boolean
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
  /** 可观测持仓窗口内日 K 最低价相对入场价的最大不利偏移；<=0；open_t+1 含入场日、close_t 自下一交易日起，退出日不计入；日内区间口径的诊断量，不代表可成交实现收益；旧结果/不可得为 null */
  mae_pct?: number | null
  /** 可观测持仓窗口内日 K 最高价相对入场价的最大有利偏移；>=0；观测窗口口径同 mae_pct */
  mfe_pct?: number | null
}

export interface StrategyBacktestRequest {
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
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: 'equal' | 'score_weight' | 'equal_vol' | 'risk_parity' | 'mean_variance' | 'max_diversification'
  mode?: 'position' | 'full'
  holding_days?: number
  regime_filter?: { states?: string[]; min_score?: number } | null
  benchmark_symbol?: '000001.INDEX' | '000300.INDEX' | '000905.INDEX' | '000852.INDEX'
  risk_free_rate?: number
}

export interface WalkForwardFold {
  train_start: string
  train_end: string
  oos_start: string
  oos_end: string
  n_candidates: number
  selected_label: string
  selected_params: Record<string, any>
  train_stats: Record<string, any>
  oos_stats: Record<string, any>
  degradation: number | null
  oos_curve: Array<{ date: string; value: number }>
  error?: string | null
}

export interface WalkForwardResult {
  /** 未启用标记: false = 未运行 (结构化空块); 旧持久化响应缺省该字段 — undefined 视为已启用 */
  enabled?: boolean
  scheme: string
  selection_metric: string
  candidate_space: string
  n_candidates: number
  /** 执行预算元数据 (enabled 响应才有): 请求候选数 / 截断后实际候选数 / 额外回测执行上限 */
  requested_candidates?: number
  effective_candidates?: number
  max_executions?: number
  warning?: string | null
  folds: WalkForwardFold[]
  stitched_curve: Array<{ date: string; value: number }>
  summary: {
    metric: string
    n_folds: number
    positive_return_folds: number
    positive_fold_ratio: number | null
    worst_fold_return: number | null
    mean_oos_return: number | null
    mean_degradation: number | null
    oos_total_return: number | null
    oos_sharpe: number | null
    oos_max_drawdown: number | null
    metric_context?: Record<string, any>
  }
  param_drift: {
    n_distinct_param_sets: number
    distinct_labels: string[]
    params: Record<string, Array<number | null>>
  }
}

export interface StrategyRobustnessResult {
  run_id: string
  full_stats: Record<string, any>
  random_seed: number
  segment_stability: {
    folds: Array<{ start: string; end: string; stats: Record<string, any>; error?: string | null }>
    summary: { metric: string; n_folds: number; mean: number; std: number; worst: number; positive_folds: number }
  }
  walk_forward?: WalkForwardResult
  bootstrap?: {
    sharpe: number
    ci_low: number
    ci_high: number
    ci: number
    n_boot: number
  }
  mc_permutation?: {
    p_value: number
    n_perm: number
    observed_sharpe: number
  }
  parameter_perturbation?: {
    fraction: number
    baseline: Record<string, number | null>
    cases: Array<{
      param: string
      label: string
      direction: 'down' | 'up'
      base_value: number
      value: number
      stats: Record<string, number | null>
      error?: string | null
    }>
    reason?: string | null
  }
  exit_breakdown: Array<{
    exit_reason: string
    n: number
    win_rate: number
    avg_pnl_pct: number
    total_pnl_pct: number
  }>
  warnings?: string[]
  data_snapshot?: BacktestDataSnapshot
  methodology_context?: string
}

export interface BrinsonAttributionGroup {
  group: string
  portfolio_weight: number | null
  benchmark_weight: number | null
  portfolio_return: number | null
  benchmark_return: number | null
  allocation: number | null
  selection: number | null
  interaction: number | null
  total_effect: number | null
}

export interface TradeIndustryAttribution {
  status: string
  reason?: string
  scope: string
  classification_note: string
  input_trades: number
  classified_trades: number
  capital_coverage: number | null
  warnings: string[]
  brinson: {
    status: string
    normalized: boolean
    portfolio_return: number | null
    benchmark_return: number | null
    excess_return: number | null
    allocation: number | null
    selection: number | null
    interaction: number | null
    total_effect: number | null
    groups: BrinsonAttributionGroup[]
  } | null
  fama_french: {
    status: string
    reason: string
    detail: string
    alpha: number | null
    betas: Record<string, number>
    contributions: Record<string, number>
    r_squared: number | null
    residual_volatility: number | null
    observations: number
  }
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
  attribution?: TradeIndustryAttribution | null
  elapsed_ms: number
  error: string | null
  methodology_context?: string
  warnings?: string[]
  data_snapshot?: BacktestDataSnapshot
  metric_context?: BacktestMetricContext
  engine_version?: string
  random_seed?: number | null
  /** 后端是否已将完整结果保存为 BacktestRun；false 时不能下载 Run 报告。 */
  persisted?: boolean
}

// ===== Backtest Run 持久化历史 (/api/backtest/runs) =====
// 后端契约: backend/app/backtest/run_store.py (schema_version=1)。
// Run 为不可变事实记录，仅 favorite/label 可经 PATCH 修改。

export type BacktestRunKind = 'strategy' | 'factor' | 'composite'

export interface BacktestRunSubject {
  id: string
  name: string
  hash: string
}

/** 列表/比较用的轻量摘要 — 不携带曲线与交易明细 */
export interface BacktestRunSummary {
  run_id: string
  kind: BacktestRunKind
  status: string
  created_at: string
  subject: BacktestRunSubject
  start: string | null
  end: string | null
  symbols_count: number | null
  favorite: boolean
  label: string
  source_run_id: string | null
  /** 头部指标子集(策略: total_return/sharpe 等; 因子: ic_mean/ir) */
  stats: Record<string, number>
  n_trades: number
  n_points: number
  has_factor_result: boolean
  has_csv_export: boolean
  warnings_count: number
}

export interface BacktestRunListResponse {
  items: BacktestRunSummary[]
  total: number
  limit: number
  offset: number
}

/**
 * 完整 Run — GET /api/backtest/runs/{run_id}。
 * 曲线/交易明细可能很大; 旧引擎记录 equity 键, 新引擎为 value 键, 两者均需兼容读取。
 */
export interface BacktestRun {
  schema_version: number
  run_id: string
  kind: BacktestRunKind
  created_at: string
  status: string
  subject: BacktestRunSubject
  config: Record<string, any>
  data_snapshot: Record<string, any>
  benchmark: { symbol?: string | null; name?: string | null } | null
  cost_model: Record<string, any>
  metric_context: Record<string, any>
  random_seed: number | null
  engine_version: string
  stats: Record<string, any>
  equity_curve: { date: string; value?: number; equity?: number; cash?: number; positions?: number; exposure?: number }[]
  drawdown_curve: { date: string; value?: number }[]
  benchmark_curve: { date: string; value?: number; close?: number; name?: string; symbol?: string }[]
  trades: Record<string, any>[]
  per_symbol_stats: Record<string, any>[]
  factor_result: Record<string, any> | null
  attribution?: TradeIndustryAttribution | null
  warnings: string[]
  favorite: boolean
  label: string
  source_run_id: string | null
}

/** 配置差异条目 — 相对 baseline 的单条差异；op: added(新增)/removed(移除)/changed(修改) */
export interface BacktestRunConfigDiffEntry {
  path: string
  op: 'added' | 'removed' | 'changed'
  before: unknown
  after: unknown
}

/** 单个 candidate 相对 baseline 的配置差异（条目受限，total 完整） */
export interface BacktestRunConfigDiffCandidate {
  run_id: string
  total: number
  truncated: boolean
  entries: BacktestRunConfigDiffEntry[]
}

export interface BacktestRunConfigDiff {
  baseline_run_id: string
  candidates: BacktestRunConfigDiffCandidate[]
}

/** 交易样本行（新增/消失） */
export interface BacktestRunTradeSample {
  symbol: string | null
  entry_date: string | null
  exit_date: string | null
  shares: number | null
  entry_value: number | null
  exit_value: number | null
  pnl_pct: number | null
}

/** 共同交易样本：份额/金额任一不同则 value_differs=true（仍属共同） */
export interface BacktestRunTradeCommonSample {
  symbol: string | null
  entry_date: string | null
  exit_date: string | null
  value_differs: boolean
  baseline: { shares: number | null; entry_value: number | null; exit_value: number | null; pnl_pct: number | null }
  candidate: { shares: number | null; entry_value: number | null; exit_value: number | null; pnl_pct: number | null }
}


export interface BacktestRunTradeSummaryCandidate {
  run_id: string
  n_trades: number
  common: number
  common_value_diff: number
  added: number
  removed: number
  samples: {
    common: BacktestRunTradeCommonSample[]
    added: BacktestRunTradeSample[]
    removed: BacktestRunTradeSample[]
  }
}

export interface BacktestRunTradeSummary {
  baseline_run_id: string
  baseline_n_trades: number
  candidates: BacktestRunTradeSummaryCandidate[]
}

/** POST /api/backtest/runs/compare 响应 — 指标矩阵 + 原值曲线 + 可比性警告 + 配置/交易差异 */
export interface BacktestRunComparison {
  runs: BacktestRunSummary[]
  metric_matrix: Record<string, Record<string, number | null>>
  curves: {
    run_id: string
    kind: BacktestRunKind
    equity_curve: BacktestRun['equity_curve']
    benchmark_curve: BacktestRun['benchmark_curve']
  }[]
  warnings: string[]
  /** 相对第一个 run (baseline) 的递归配置差异 — additive 字段，旧后端响应可能缺省 */
  config_diff?: BacktestRunConfigDiff
  /** 相对 baseline 的交易集合差异（共同/新增/消失）— additive 字段，旧后端响应可能缺省 */
  trade_summary?: BacktestRunTradeSummary
}

// ===== Strategy experiments / cross-section / signal scorecard =====
export interface CompositeStrategyInput {
  strategy_id: string
  name: string
  description?: string
  children: Array<{ strategy_id: string; weight: number }>
  merge_mode: 'union' | 'intersect'
  min_confirm: number
  mode: 'create' | 'update'
}

export interface ParameterGridRequest {
  strategy_id: string
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  params?: Record<string, number> | null
  grid: Record<string, number[]>
  objective: 'sharpe' | 'calmar' | 'total_return' | 'risk_adjusted'
  max_scenarios?: number
  matching?: 'close_t' | 'open_t+1'
  holding_days?: number
  regime_filter?: { states?: string[]; min_score?: number } | null
  risk_free_rate?: number
}

export interface ParameterGridLaunchResponse {
  experiment_id: string
  config_hash: string
  scenario_count: number
  requested_count?: number
  truncated: boolean
  objective?: string
  status: 'started' | 'already_running'
}

export interface ParameterGridScenario {
  scenario_id: string
  params: Record<string, number>
  stats: Record<string, number>
  score: number | null
  rank: number
  error: string | null
  elapsed_ms: number
  /** 严格三目标 Pareto 层：1 为非支配层；旧实验或不合格场景可能缺失。 */
  pareto_front?: number | null
}

export interface ParameterGridExperiment {
  experiment_id: string
  config_hash: string
  strategy_id: string
  objective: string
  base_config: Record<string, unknown>
  grid: Record<string, number[]>
  requested_count: number
  scenario_count: number
  max_scenarios: number
  truncated: boolean
  status: 'pending' | 'running' | 'completed' | 'cancelled' | 'failed'
  scenarios: ParameterGridScenario[]
  best_scenario_id: string | null
  robustness: Record<string, unknown> | null
  created_at: string
  updated_at: string
  completed: number
  total: number
}

export interface SignalScorecardTrackedItem {
  signal_key: string
  signal_name: string
  signal_kind: string
  direction: 'up' | 'not_up'
  enabled: boolean
}

export interface SignalScorecardEvent {
  id: string
  signal_key: string
  signal_name: string
  signal_kind: string
  source: string
  symbol: string
  name?: string
  date: string
  anchor_price: number | null
  direction_expected: 'up' | 'not_up'
  created_ts: number
  context: Record<string, unknown>
}

export interface SignalScorecardStat {
  signal_key: string
  horizon: number
  total: number
  completed: number
  pending: number
  hit_count: number
  miss_count: number
  neutral_count: number
  hit_rate_pct: number | null
  avg_return_pct: number | null
  sample_size: number
}

export interface SignalScorecardOutcome {
  horizon: number
  eval_status: 'pending' | 'completed' | 'unable'
  outcome: 'hit' | 'miss' | 'neutral' | null
  direction_correct: boolean | null
  stock_return_pct: number | null
  end_close: number | null
  unable_reason: string | null
  evaluated_ts: number | null
}

export interface CrossCorrelationResponse {
  selected: string
  peers: string[]
  industry: string | null
  window: number
  minSamples: number
  alignedDays: number
  pairRows: Array<{
    peer: string
    correlation: number | null
    covariance: number | null
    beta: number | null
    samples: number | null
    previousCorrelation: number | null
    correlationDelta: number | null
  }>
  matrix: {
    instruments: string[]
    correlation: Array<Array<number | null>>
    covariance: Array<Array<number | null>>
    samples: Array<Array<number | null>>
  }
  averageCorrelation: number | null
  boundaryNotes: string[]
}

export interface CrossRelativeStrengthResponse {
  selected: string
  summary: {
    label: string
    detail: string
    tone: 'bull' | 'risk' | 'neutral'
    latestDate: string | null
    dataLimitations: string[]
  }
  benchmarks: Array<{
    key: string
    label: string
    latestRelativePct: number | null
    points: Array<{ date: string; stockNav: number; benchmarkNav: number; relativePct: number }>
  }>
  windows: Array<{
    days: number
    label: string
    stockReturnPct: number | null
    benchmarks: Array<{
      key: string
      label: string
      returnPct: number | null
      relativeReturnPct: number | null
    }>
  }>
  boundaryNotes: string[]
}

export interface CrossPeerResponse {
  selected: string
  mode: string
  sortKey: string
  universe: string | null
  rows: Array<Record<string, unknown> & { symbol?: string; name?: string; isCurrent?: boolean }>
  allRows: Array<Record<string, unknown>>
  summary: {
    total: number
    displayed: number
    averages: Record<string, number | null>
    currentRank: number | null
    currentTotal: number
  }
  boundaryNotes: string[]
}

export interface CrossReverseScreenResponse {
  selected: string
  request: { conditions: Array<Record<string, unknown>>; order_by: Record<string, string>; limit: number } | null
  result: { rows?: Array<Record<string, unknown>>; total?: number } | null
  reasons: string[]
  features: Record<string, unknown>
  boundaryNotes: string[]
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
  /** 受控外部行情降级(默认关闭); scopes 仅 realtime/depth 白名单, 首批仅 realtime */
  external_fallback_enabled?: boolean
  external_fallback_scopes?: string[]
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

export interface JournalFholdPreviewRow {
  account_id: string
  date: string
  time: string
  symbol: string
  name: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  amount: number
  fee: number
}

export interface JournalFholdPreview {
  available: boolean
  snapshot_sha256: string | null
  row_count: number
  importable_count: number
  skipped_count: number
  accounts: { id: string; name: string; fills: number }[]
  preview_rows: JournalFholdPreviewRow[]
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
    source?: string
    mode: 'replace' | 'append'
    account_id: string
    new_fills: number
    deduped_fills: number
    deduped_events: number
    conflicting_fills?: number
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

// ===== 实时行情状态 (GET /api/intraday/status) =====

/** 行情数据健康状态 — 与后端 /api/intraday/status 契约一致 */
export type QuoteDataState = 'disabled' | 'warming_up' | 'ready' | 'empty' | 'error' | 'stale'

export interface QuoteStatusResponse {
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
  // 数据健康契约追加字段 — 旧后端可能缺失, 全部 optional; 经 resolveQuoteDataState 安全降级
  data_state?: QuoteDataState
  has_recent_data?: boolean
  total_symbol_count?: number
  last_error_code?: 'provider_empty' | 'provider_error' | null
  source_as_of?: string | null
}

/** 数据新鲜度阈值 — 与后端一致: max(2 * interval_s, 30s) */
export function quoteRecentThresholdMs(intervalS: number | null | undefined): number {
  return Math.max(2 * (intervalS ?? 0), 30) * 1000
}

/**
 * 解析行情数据状态。新后端直接采用 data_state;
 * 旧后端缺该字段时从旧字段降级推断 — 绝不把轮询线程存活当作数据健康。
 */
export function resolveQuoteDataState(s: QuoteStatusResponse | null | undefined): QuoteDataState | null {
  if (!s) return null
  if (s.data_state) return s.data_state
  if (!s.enabled) return 'disabled'
  if (s.quote_age_ms != null && s.quote_age_ms <= quoteRecentThresholdMs(s.interval_s)) return 'ready'
  if (s.last_fetch_ms == null && s.quote_age_ms == null) return 'warming_up'
  return 'stale'
}

const QUOTE_DATA_STATE_TEXT: Record<QuoteDataState, string> = {
  disabled: '未开启',
  warming_up: '正在获取首批数据',
  empty: '轮询中但数据源未返回行情',
  error: '行情源暂不可用',
  stale: '行情已过期',
  ready: '行情已更新',
}

export function quoteDataStateText(state: QuoteDataState | null | undefined): string {
  return state ? QUOTE_DATA_STATE_TEXT[state] : '状态未知'
}

/** source_as_of 非本日时返回 "本地快照截至 YYYY-MM-DD"; 本日/缺失/不可解析时返回 null。 */
export function quoteSnapshotText(sourceAsOf: string | null | undefined, now: Date = new Date()): string | null {
  if (!sourceAsOf) return null
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(sourceAsOf.trim())
  if (!m) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  return m[1] === today ? null : `本地快照截至 ${m[1]}`
}

// ===== Market Data（上游已发布只读快照） =====
// `/api/market-data` 的 rows 直接透传 provider DataFrame 字段；此处仅描述已发布字段，
// 不对日期、金额、side/direction 等上游语义作二次归一化。

export type MarketDataFrequency = 'daily' | 'minute'
export type MarketDataCallAuctionSession = 'open' | 'close'

export type MarketDataCapabilityKey =
  | 'chip'
  | 'moneyflow_daily_stock'
  | 'moneyflow_daily_block'
  | 'moneyflow_minute_stock'
  | 'moneyflow_minute_block'
  | 'call_auction'
  | 'transactions'
  | 'hk_adjustment'
  | 'hk_financial'

export interface MarketDataCapability {
  available: boolean
  source: string | null
  earliest_date: string | null
  latest_date: string | null
  rows: number | null
  symbols: number | null
  reason: string | null
}

export type MarketDataCapabilities = Record<MarketDataCapabilityKey, MarketDataCapability>

/** status 在旧服务或部署切换窗口可缺 capabilities；消费方须按 unavailable 防御展示。 */
export interface MarketDataStatusResponse {
  available?: boolean
  source?: string | null
  provider?: string | null
  capabilities?: Partial<MarketDataCapabilities>
}

export interface MarketDataResponse<Row> {
  available: boolean
  source: string | null
  rows: Row[]
  reason?: string | null
}

export interface MarketDataChipRow {
  symbol: string
  trade_date: string
  peak_price: number | null
  peak_volume: number | null
  peak_ratio: number | null
  profit_ratio: number | null
  avg_cost: number | null
  concentration_90: number | null
  range_90_low: number | null
  range_90_high: number | null
  concentration_70: number | null
  range_70_low: number | null
  range_70_high: number | null
  cr10: number | null
  cr30: number | null
  gini: number | null
  main_peak_price: number | null
  main_peak_volume: number | null
  main_peak_ratio: number | null
  main_concentration: number | null
  retail_peak_price: number | null
  retail_peak_volume: number | null
  retail_peak_ratio: number | null
  retail_concentration: number | null
  has_retail_peak: boolean | null
  peak_count: number | null
  window_days: number | null
  price_step: number | null
  asset_type: number | null
  source: string
}

export interface MarketDataMoneyflowStockRow {
  symbol?: string
  trade_date?: string
  bucket_time?: string | null
  total_amount?: number | null
  inflow_amount?: number | null
  outflow_amount?: number | null
  net_amount?: number | null
  super_large_net?: number | null
  large_net?: number | null
  medium_net?: number | null
  small_net?: number | null
  main_traditional_net?: number | null
  main_broad_net?: number | null
  retail_net?: number | null
  neutral_net?: number | null
  unknown_net?: number | null
  valid_count?: number | null
  invalid_count?: number | null
  unknown_count?: number | null
  source?: string | null
}

export interface MarketDataMoneyflowBlockRow extends MarketDataMoneyflowStockRow {
  block_type?: number | null
  block_code?: string | null
  block_name?: string | null
}
export interface MarketDataCallAuctionRow {
  event_time: string
  price: number | null
  volume: number | null
  amount: number | null
  direction: number | null
  session: MarketDataCallAuctionSession
  venue: string | null
  source: string
}

export interface MarketDataTransactionRow {
  symbol: string
  datetime: string
  price: number | null
  volume: number | null
  amount: number | null
  direction: number | null
  order_count: number | null
  venue: string | null
  source: string
}

export interface MarketDataChipResponse extends MarketDataResponse<MarketDataChipRow> {
  symbol: string
  start: string
  end: string
  limit: number
}

export interface MarketDataMoneyflowStockResponse extends MarketDataResponse<MarketDataMoneyflowStockRow> {
  symbol: string
  freq: MarketDataFrequency
  start: string
  end: string
}

export interface MarketDataMoneyflowBlocksResponse extends MarketDataResponse<MarketDataMoneyflowBlockRow> {
  freq: MarketDataFrequency
  date: string
  block_type: number | null
  limit: number
}

export interface MarketDataCallAuctionResponse extends MarketDataResponse<MarketDataCallAuctionRow> {
  symbol: string
  date: string
  session: MarketDataCallAuctionSession | null
  limit: number
}

export interface MarketDataTransactionsResponse extends MarketDataResponse<MarketDataTransactionRow> {
  symbol: string
  date: string
  limit: number
}

export interface MarketDataChipRequest {
  start: string
  end: string
  limit?: number
}

export interface MarketDataMoneyflowStockRequest {
  freq: MarketDataFrequency
  start: string
  end: string
}

export interface MarketDataMoneyflowBlocksRequest {
  freq: MarketDataFrequency
  date: string
  blockType?: number
  limit?: number
}

export interface MarketDataCallAuctionRequest {
  date: string
  session?: MarketDataCallAuctionSession
  limit?: number
}

export interface MarketDataTransactionsRequest {
  date: string
  limit?: number
}

// ===== Research analysis（canonical enriched 日 K，只读） =====

export interface ResearchAnalysisRiskResult {
  status: string
  observations: number
  minSamples: number
  descriptive: {
    mean: number | null
    std: number | null
    annualizedVolatility: number | null
    skewness: number | null
    excessKurtosis: number | null
    min: number | null
    max: number | null
  }
  historicalVar: number | null
  historicalCvar: number | null
  parametricVar: number | null
}

export interface ResearchAnalysisPerformanceResult {
  status: string
  sortino?: number | null
  omega?: number | null
  max_drawdown?: number | null
  calmar?: number | null
  ulcer_index?: number | null
}

export interface ResearchAnalysisAdfResult {
  status: string
  adf_statistic?: number | null
  p_value?: number | null
  lags_used?: number | null
  is_stationary?: boolean | null
  observations?: number | null
}

export interface ResearchAnalysisGarchResult {
  status: string
  current_volatility?: number | null
  long_run_volatility?: number | null
  persistence?: number | null
  observations?: number | null
}

export interface ResearchSymbolAnalysisResult {
  risk: ResearchAnalysisRiskResult
  performance: ResearchAnalysisPerformanceResult
  statistics: {
    adf: ResearchAnalysisAdfResult
    garch: ResearchAnalysisGarchResult
  }
}

export interface ResearchSymbolAnalysisAvailableResponse {
  available: true
  source: string
  symbol: string
  start: string
  end: string
  data_as_of: string | null
  observations: number
  result: ResearchSymbolAnalysisResult
  warnings: string[]
  reason: null
}

export interface ResearchSymbolAnalysisUnavailableResponse {
  available: false
  source: null
  symbol: string
  start: null
  end: null
  data_as_of: null
  observations: 0
  result: null
  warnings: string[]
  reason: string
}

export type ResearchSymbolAnalysisResponse =
  | ResearchSymbolAnalysisAvailableResponse
  | ResearchSymbolAnalysisUnavailableResponse

export interface ResearchSymbolAnalysisRequest {
  start?: string
  end?: string
}

// ===== Research (假设注册 + 定时研究) =====
// 后端: api/research.py + services/research_registry.py + scheduled_research.py
// 假设状态机与证据 kind 以后端 STATUSES / EVIDENCE_KINDS 为准。

export type ResearchHypothesisStatus =
  | 'exploring'
  | 'testing'
  | 'validated'
  | 'rejected'
  | 'monitoring'

export type ResearchEvidenceKind = 'backtest' | 'note' | 'observation'

export type ResearchScheduleTemplate =
  | 'market_recap_daily'
  | 'watchlist_recap_daily'
  | 'strategy_pool_weekly'

export interface ResearchEvidence {
  ts: string
  kind: ResearchEvidenceKind | string
  ref: string
  summary: string
}

export interface ResearchHypothesis {
  id: string
  title: string
  thesis: string
  status: ResearchHypothesisStatus | string
  tags: string[]
  evidence: ResearchEvidence[]
  created_at: string
  updated_at: string
}

export interface ResearchRunCard {
  run_id: string
  kind: string
  config: Record<string, unknown>
  config_hash: string
  strategy_hash: string
  stats: Record<string, unknown>
  created_at: string
}

export interface ResearchSchedule {
  id: string
  name: string
  template: ResearchScheduleTemplate | string
  cron: string
  enabled: boolean
  params: Record<string, unknown>
  created_at: string
  updated_at: string
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
}

export interface ResearchScheduleRunResult {
  title?: string
  summary?: string
  artifacts?: unknown[]
  warnings?: string[]
  [key: string]: unknown
}

export interface ResearchScheduleRunNowResponse {
  schedule: ResearchSchedule
  result: ResearchScheduleRunResult
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
  journalLedger: () => request<JournalLedger | null>('/api/journal/ledger'),
  journalDelete: () => request<{ deleted: boolean }>('/api/journal/ledger', { method: 'DELETE' }),
  journalFeedback: (rating: 'helpful' | 'not_helpful') =>
    request<{ ok: boolean }>('/api/journal/feedback', {
      method: 'POST',
      body: JSON.stringify({ rating }),
    }),
  journalFholdPreview: () => request<JournalFholdPreview>('/api/journal/fhold-preview'),
  journalFholdImport: (snapshotSha256: string, benchmark: string, narrative: boolean) =>
    request<JournalLedger>('/api/journal/fhold-import', {
      method: 'POST',
      body: JSON.stringify({
        snapshot_sha256: snapshotSha256,
        benchmark,
        narrative,
      }),
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
    request<{ ok: boolean; error?: string; category?: string; model?: string; provider?: string; response?: string; latency_ms?: number }>(
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
  /** 受控外部行情降级开关 — 开启时 scopes 固定 ["realtime"], 关闭置空; 返回清洗后的两字段(非法 scope 400) */
  updateExternalFallback: (enabled: boolean, scopes: string[]) =>
    request<{ external_fallback_enabled: boolean; external_fallback_scopes: string[] }>(
      '/api/settings/preferences/external-fallback',
      {
        method: 'PUT',
        body: JSON.stringify({ external_fallback_enabled: enabled, external_fallback_scopes: scopes }),
      },
    ),
  quoteStatus: () =>
    request<QuoteStatusResponse>('/api/intraday/status'),
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
    request<IndexQuotesResponse>(
      `/api/intraday/indices${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`,
    ),
  /** 只读实时快照（本地优先 / 受控外部 fallback），最多 60 个 symbol；仅供展示 */
  intradaySnapshot: (symbols?: string[]) =>
    request<IntradaySnapshotResponse>(
      `/api/intraday/snapshot${symbols?.length ? `?symbols=${encodeURIComponent(symbols.slice(0, 60).join(','))}` : ''}`,
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
  instrumentSearch: (q: string, limit = 20, assetTypes?: readonly InstrumentAssetType[]) => {
    const params = new URLSearchParams({ q, limit: String(limit) })
    assetTypes?.forEach((assetType) => params.append('asset_type', assetType))
    return request<{ results: InstrumentSearchResult[] }>(
      `/api/kline/instruments/search?${params.toString()}`,
    )
  },

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
    request<{
      as_of: string | null
      results: Record<string, Pick<ScreenerResult, 'total' | 'as_of' | 'rows'>>
      today_ever_matched: Record<string, string[]> | null
      today_ever_rows: Record<string, Record<string, ScreenerResult['rows'][number]>> | null
      updated_at: number | null
      canonical_as_of?: string | null
      discarded_as_of?: string | null
    }>(
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
    risk_free_rate?: number
  }) =>
    request<FactorBacktestResult>('/api/backtest/factor/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  strategyBacktestRun: (payload: StrategyBacktestRequest) =>
    request<StrategyBacktestResult>('/api/backtest/strategy/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  strategyRobustness: (
    payload: StrategyBacktestRequest & {
      n_folds?: number
      bootstrap?: boolean
      mc_permutation?: boolean
      n_boot?: number
      n_perm?: number
      seed?: number | null
      parameter_perturbation?: boolean
      perturbation_pct?: number
      max_perturbed_params?: number
      walk_forward_enabled?: boolean
    },
  ) =>
    request<StrategyRobustnessResult>('/api/backtest/strategy/robustness', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  parameterGridLaunch: (payload: ParameterGridRequest) =>
    request<ParameterGridLaunchResponse>('/api/backtest/parameter-grid', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  parameterGridGet: (experimentId: string) =>
    request<ParameterGridExperiment | null>(
      `/api/backtest/parameter-grid/${encodeURIComponent(experimentId)}`,
      undefined,
      { silent404: true },
    ),

  parameterGridCancel: (experimentId: string) =>
    request<{ ok: boolean; experiment_id?: string; message?: string }>(
      `/api/backtest/parameter-grid/${encodeURIComponent(experimentId)}/cancel`,
      { method: 'POST' },
    ),

  // ===== Backtest Run 持久化历史 =====
  backtestRuns: (opts?: { kind?: BacktestRunKind; favorite?: boolean; query?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (opts?.kind) qs.set('kind', opts.kind)
    if (opts?.favorite != null) qs.set('favorite', String(opts.favorite))
    if (opts?.query) qs.set('query', opts.query)
    if (opts?.limit != null) qs.set('limit', String(opts.limit))
    if (opts?.offset != null) qs.set('offset', String(opts.offset))
    const suffix = qs.toString()
    return request<BacktestRunListResponse>(`/api/backtest/runs${suffix ? `?${suffix}` : ''}`)
  },

  backtestRunGet: (runId: string) =>
    request<BacktestRun>(`/api/backtest/runs/${encodeURIComponent(runId)}`),

  /** 仅 favorite/label 可变, 后端拒绝其他字段 */
  backtestRunPatch: (runId: string, body: { favorite?: boolean; label?: string }) =>
    request<BacktestRun>(`/api/backtest/runs/${encodeURIComponent(runId)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  /** 旧 run_card 只读迁移项会被后端 403 拒绝 */
  backtestRunDelete: (runId: string) =>
    request<{ ok: boolean }>(`/api/backtest/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),

  backtestRunsCompare: (runIds: string[]) =>
    request<BacktestRunComparison>('/api/backtest/runs/compare', {
      method: 'POST',
      body: JSON.stringify({ run_ids: runIds }),
    }),

  /** 按原 config 重新运行, 返回带 source_run_id 的新 Run */
  backtestRunRerun: (runId: string) =>
    request<BacktestRun>(`/api/backtest/runs/${encodeURIComponent(runId)}/rerun`, { method: 'POST' }),

  /** 导出为浏览器直接下载 (Content-Disposition: attachment), 无需 request 封装 */
  backtestRunExportUrl: (runId: string, fmt: 'json' | 'csv') =>
    `/api/backtest/runs/${encodeURIComponent(runId)}/export?fmt=${fmt}`,

  pipelineRun: () => request<{ job_id: string; reused: boolean }>(
    '/api/pipeline/run', { method: 'POST' },
  ),
  pipelineJob: (id: string) => request<PipelineJob>(`/api/pipeline/jobs/${id}`),
  pipelineJobs: (limit = 20) =>
    request<{ active_id: string | null; jobs: PipelineJobSummary[] }>(
      `/api/pipeline/jobs?limit=${limit}`,
    ),

  dataStatus: () => request<DataStatus>('/api/data/status'),
  canonicalHistoryStatus: () =>
    request<CanonicalHistoryStatus>('/api/data/canonical-history/status'),
  canonicalHistoryBackfill: (body: CanonicalHistoryBackfillRequest = {}) =>
    request<CanonicalHistoryBackfillResponse>('/api/data/canonical-history/backfill', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
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

  financialMetrics: (symbol: string) =>
    request<{ data: FinancialMetricRecord[] }>(
      `/api/financials/metrics?symbol=${encodeURIComponent(symbol)}`,
    ),

  financialIncome: (symbol: string) =>
    request<{ data: FinancialIncomeRecord[] }>(
      `/api/financials/income?symbol=${encodeURIComponent(symbol)}`,
    ),

  financialBalanceSheet: (symbol: string) =>
    request<{ data: FinancialBalanceSheetRecord[] }>(
      `/api/financials/balance-sheet?symbol=${encodeURIComponent(symbol)}`,
    ),

  financialCashFlow: (symbol: string) =>
    request<{ data: FinancialCashFlowRecord[] }>(
      `/api/financials/cash-flow?symbol=${encodeURIComponent(symbol)}`,
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
    request<{
      reports: AiReviewReport[]
      canonical_as_of: string | null
      discarded_reports: Array<{ id: string | null; as_of: string | null }>
    }>('/api/market-recap/reports'),

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

  strategySaveComposite: (payload: CompositeStrategyInput) =>
    request<{ ok: boolean; strategy_id: string; source: 'composite'; path: string }>(
      '/api/strategies/composite/save',
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  /** 删除自定义策略（内置策略不可删除） */
  strategyDelete: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/${strategyId}`, { method: 'DELETE' }),

  strategyReload: () =>
    request<{ ok: boolean; count: number }>('/api/strategies/reload', { method: 'POST' }),

  // ===== Cross-section research =====
  crossCorrelation: (symbol: string, window = 120) =>
    request<CrossCorrelationResponse>(
      `/api/cross-section/correlation?symbol=${encodeURIComponent(symbol)}&window=${window}`,
    ),

  crossRelativeStrength: (symbol: string, days = 120) =>
    request<CrossRelativeStrengthResponse>(
      `/api/cross-section/relative-strength?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),

  crossPeerComparison: (symbol: string, mode = 'industry', sortKey = 'amount') =>
    request<CrossPeerResponse>(
      `/api/cross-section/peer-comparison?symbol=${encodeURIComponent(symbol)}&mode=${encodeURIComponent(mode)}&sort_key=${encodeURIComponent(sortKey)}`,
    ),

  crossReverseScreen: (symbol: string) =>
    request<CrossReverseScreenResponse>(
      `/api/cross-section/reverse-screen?symbol=${encodeURIComponent(symbol)}`,
    ),

  // ===== Signal scorecard =====
  signalScorecardTracked: () =>
    request<{ items: SignalScorecardTrackedItem[] }>('/api/signal-scorecard/tracked-signals'),

  signalScorecardUpdateTracked: (items: SignalScorecardTrackedItem[]) =>
    request<{ items: SignalScorecardTrackedItem[] }>('/api/signal-scorecard/tracked-signals', {
      method: 'PUT',
      body: JSON.stringify({ items }),
    }),

  signalScorecardStats: (signalKey?: string, horizon?: number) => {
    const params = new URLSearchParams()
    if (signalKey) params.set('signal_key', signalKey)
    if (horizon) params.set('horizon', String(horizon))
    const qs = params.toString()
    return request<{ stats: SignalScorecardStat[]; neutral_band_pct: number; horizons: number[] }>(
      `/api/signal-scorecard/stats${qs ? `?${qs}` : ''}`,
    )
  },

  signalScorecardEvents: (filters?: {
    signal_key?: string
    symbol?: string
    status?: 'pending' | 'mature'
    limit?: number
  }) => {
    const params = new URLSearchParams()
    if (filters?.signal_key) params.set('signal_key', filters.signal_key)
    if (filters?.symbol) params.set('symbol', filters.symbol)
    if (filters?.status) params.set('status', filters.status)
    params.set('limit', String(filters?.limit ?? 200))
    return request<{ events: SignalScorecardEvent[]; total: number }>(
      `/api/signal-scorecard/events?${params}`,
    )
  },

  signalScorecardEventDetail: (eventId: string) =>
    request<{ event: SignalScorecardEvent; outcomes: SignalScorecardOutcome[]; status: 'pending' | 'mature' }>(
      `/api/signal-scorecard/events/${encodeURIComponent(eventId)}/outcomes`,
    ),

  signalScorecardEvaluate: () =>
    request<Record<string, number | boolean>>('/api/signal-scorecard/evaluate', { method: 'POST' }),

  signalScorecardBackfill: (signalKeys: string[], dateFrom: string, dateTo: string) => {
    const params = new URLSearchParams({
      signal_keys: signalKeys.join(','),
      date_from: dateFrom,
      date_to: dateTo,
    })
    return request<Record<string, number | boolean>>(
      `/api/signal-scorecard/backfill?${params}`,
      { method: 'POST' },
    )
  },

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

  // ===== Market Data（上游发布快照，只读查询） =====
  marketDataStatus: () =>
    request<MarketDataStatusResponse>('/api/market-data/status'),

  marketDataChip: (symbol: string, params: MarketDataChipRequest) => {
    const qs = new URLSearchParams({ start: params.start, end: params.end })
    if (params.limit != null) qs.set('limit', String(params.limit))
    return request<MarketDataChipResponse>(
      `/api/market-data/chip/${encodeURIComponent(symbol)}?${qs.toString()}`,
    )
  },

  marketDataMoneyflowStock: (symbol: string, params: MarketDataMoneyflowStockRequest) => {
    const qs = new URLSearchParams({ freq: params.freq, start: params.start, end: params.end })
    return request<MarketDataMoneyflowStockResponse>(
      `/api/market-data/moneyflow/stock/${encodeURIComponent(symbol)}?${qs.toString()}`,
    )
  },

  marketDataMoneyflowBlocks: (params: MarketDataMoneyflowBlocksRequest) => {
    const qs = new URLSearchParams({ freq: params.freq, date: params.date })
    if (params.blockType != null) qs.set('block_type', String(params.blockType))
    if (params.limit != null) qs.set('limit', String(params.limit))
    return request<MarketDataMoneyflowBlocksResponse>(
      `/api/market-data/moneyflow/blocks?${qs.toString()}`,
    )
  },

  marketDataCallAuction: (symbol: string, params: MarketDataCallAuctionRequest) => {
    const qs = new URLSearchParams({ date: params.date })
    if (params.session) qs.set('session', params.session)
    if (params.limit != null) qs.set('limit', String(params.limit))
    return request<MarketDataCallAuctionResponse>(
      `/api/market-data/call-auction/${encodeURIComponent(symbol)}?${qs.toString()}`,
    )
  },

  marketDataTransactions: (symbol: string, params: MarketDataTransactionsRequest) => {
    const qs = new URLSearchParams({ date: params.date })
    if (params.limit != null) qs.set('limit', String(params.limit))
    return request<MarketDataTransactionsResponse>(
      `/api/market-data/transactions/${encodeURIComponent(symbol)}?${qs.toString()}`,
    )
  },

  // ===== Research analysis（canonical enriched 日 K，只读） =====
  researchSymbolAnalysis: (symbol: string, params: ResearchSymbolAnalysisRequest = {}) => {
    const qs = new URLSearchParams()
    if (params.start) qs.set('start', params.start)
    if (params.end) qs.set('end', params.end)
    const query = qs.toString()
    return request<ResearchSymbolAnalysisResponse>(
      `/api/research/analysis/symbol/${encodeURIComponent(symbol)}${query ? `?${query}` : ''}`,
      undefined,
      { acceptUnavailable: true },
    )
  },

  // ===== Research (假设注册 + 定时研究) =====
  researchListHypotheses: (params?: { status?: string; query?: string }) => {
    const qs = new URLSearchParams()
    if (params?.status) qs.set('status', params.status)
    if (params?.query) qs.set('query', params.query)
    const q = qs.toString()
    return request<{ items: ResearchHypothesis[] }>(`/api/research/hypotheses${q ? `?${q}` : ''}`)
  },

  researchGetHypothesis: (id: string) =>
    request<ResearchHypothesis>(`/api/research/hypotheses/${encodeURIComponent(id)}`),

  researchCreateHypothesis: (body: {
    title: string
    thesis: string
    status?: ResearchHypothesisStatus | string
    tags?: string[]
  }) =>
    request<ResearchHypothesis>('/api/research/hypotheses', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  researchUpdateHypothesis: (
    id: string,
    body: {
      title?: string
      thesis?: string
      status?: ResearchHypothesisStatus | string
      tags?: string[]
    },
  ) =>
    request<ResearchHypothesis>(`/api/research/hypotheses/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  researchAddEvidence: (
    id: string,
    body: { kind: ResearchEvidenceKind | string; ref?: string; summary: string },
  ) =>
    request<ResearchHypothesis>(`/api/research/hypotheses/${encodeURIComponent(id)}/evidence`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 404 → null(中性"未找到"语义,不弹 toast) */
  researchGetRunCard: (runId: string) =>
    request<ResearchRunCard | null>(
      `/api/research/run-cards/${encodeURIComponent(runId)}`,
      undefined,
      { silent404: true },
    ),

  researchListSchedules: () =>
    request<{ items: ResearchSchedule[] }>('/api/research/schedules'),

  researchCreateSchedule: (body: {
    name: string
    template: ResearchScheduleTemplate | string
    cron: string
    enabled?: boolean
    params?: Record<string, unknown>
  }) =>
    request<ResearchSchedule>('/api/research/schedules', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  researchUpdateSchedule: (
    id: string,
    body: {
      name?: string
      template?: ResearchScheduleTemplate | string
      cron?: string
      enabled?: boolean
      params?: Record<string, unknown>
    },
  ) =>
    request<ResearchSchedule>(`/api/research/schedules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  researchDeleteSchedule: (id: string) =>
    request<{ ok: boolean }>(`/api/research/schedules/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  researchRunScheduleNow: (id: string) =>
    request<ResearchScheduleRunNowResponse>(
      `/api/research/schedules/${encodeURIComponent(id)}/run-now`,
      { method: 'POST' },
    ),


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
    /** enriched 写入行数(新契约字段) */
    enriched_rows?: number
    /** 旧记录 fallback：日级管道历史中实为写入行数；扩展/补算类任务中确实表示天数 */
    enriched_days?: number
    index_count?: number
    index_daily_rows?: number
    etf_count?: number
    etf_daily_rows?: number
    etf_adj_factor_symbols?: number
    hk_count?: number
    hk_daily_rows?: number
    minute_rows: number
    skipped_stages?: string[]
    failed_stages?: { stage: string; error: string }[]
  } | null
  error: string | null
}

export type PipelineJobSummary = Omit<PipelineJob, 'log'>

export interface CanonicalHistoryPublished {
  generation: string
  created_at: string
  earliest_date: string | null
  latest_date: string | null
  row_count: number
  symbols: number
  trading_days: number
}

export interface CanonicalHistoryJob {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  progress_pct: number
  processed_symbols: number
  total_symbols: number
  written_rows: number
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface CanonicalHistoryStatus {
  available: boolean
  reason: string | null
  published: CanonicalHistoryPublished | null
  job: CanonicalHistoryJob | null
}

export interface CanonicalHistoryBackfillRequest {
  start_date?: string
  end_date?: string
  batch_size?: number
}

export interface CanonicalHistoryBackfillResponse {
  job_id: string
  status: 'pending' | 'running'
}

// ===== Data status =====

/** 本地增量 overlay（canonical 发布点之后的本地产出分区） */
export interface LocalOverlayStats {
  earliest_date: string | null
  latest_date: string | null
  trading_days: number
}

/** canonical 全历史权威统计（已发布 manifest）；row_count_exact=false 时 rows 为已知下界 */
export interface CanonicalHistoryStats {
  generation: string
  earliest_date: string | null
  latest_date: string | null
  rows: number
  symbols: number
  trading_days: number
}

/** 表级新鲜度 — awaiting_publish 表示上游新交易日待发布，latest_date 并非滞后 */
export interface TableFreshness {
  status: 'current' | 'awaiting_publish' | 'unknown'
  age_days: number | null
  reference_date: string | null
  reason: string | null
}

interface TableStats {
  rows: number
  row_count_exact?: boolean
  earliest_date: string | null
  latest_date: string | null
  symbols_covered: number
  trading_days: number
  available?: boolean
  source?: 'local_cache' | 'catalog_tdx_minutes'
  stage?: 'preliminary' | 'final'
  generation?: string
  logical?: string
  /** 股票池总标的数（来自维表） */
  universe_symbols?: number
  /** 最新本地单分区实际覆盖标的数（只读 symbol 列精确计算） */
  latest_partition_symbols?: number
  /** 本地增量 overlay；daily/enriched 的 earliest/latest/trading_days/symbols_covered 已合并 canonical 全历史 + overlay，代表可查询范围 */
  local_overlay?: LocalOverlayStats | null
  /** canonical 全历史权威统计 */
  canonical_history?: CanonicalHistoryStats | null
  freshness?: TableFreshness | null
  /** persisted=本地落盘；provider_on_demand=Provider 按需读取、不单独落盘 */
  storage_mode?: 'persisted' | 'provider_on_demand'
  /** 后端给出的可读状态说明（如 provider 按需说明） */
  status_message?: string | null
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
  hk_daily: TableStats | null
  hk_enriched: TableStats | null
  hk_instruments: InstrumentsStats | null
  minute: TableStats | null
  adj_factor: TableStats | null
  instruments: InstrumentsStats | null
  financials: {
    rows: number
    tables: Record<string, {
      rows: number
      symbols: number
      earliest_date?: string | null
      latest_date?: string | null
    }>
  } | null
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
  /** 最近一次管道执行摘要（新契约）；旧后端可缺失，前端回退 last_pipeline_run 展示 */
  last_pipeline?: LastPipelineSummary | null
  last_instruments_run: string | null
  checked_at: string
}

/** /api/data/status 的 last_pipeline — 最近一次管道执行结果摘要 */
export interface LastPipelineSummary {
  status: 'pending' | 'running' | 'succeeded' | 'degraded' | 'failed'
  finished_at: string | null
  error: string | null
  failed_stages: { stage: string; error: string }[]
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

export type TradeStatus = '计划中' | '建仓中' | '持仓中' | '已平仓' | '已作废'

export type TradeEventKind =
  | 'open' | 'prepare' | 'revise' | 'fill'
  | 'add' | 'trim' | 'tp' | 'sl' | 'adjust' | 'close' | 'void'

export interface TradeThesis {
  text: string
  invalidation: string
  createdAt: string
}

export interface TradePlanLeg {
  qty: number | null
  price: number | null
  total?: number | null
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
  voidedAt?: string | null
  accountId?: string
  /** prepare 事件写入的建仓计划 */
  plan?: TradePlanLeg
  /** revise 事件累积的修订历史 */
  planRevisions?: TradePlanLeg[]
  /** 分批建仓累计事实；filledAmount 不因后续减仓回退 */
  build?: {
    filledQty: number
    filledAmount: number
    fillCount: number
    completedAt: string | null
  }
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

export interface AccountSettlement {
  id: string
  ts: string
  tradeId: string
  symbol: string
  accountId: string
  realizedPnl: number
  closeDate: string
  capitalBefore: number
  capitalAfter: number
}

export interface TradingAccount {
  id: string
  currency: string
  capital: number
  horizonFundMonths: number
  maxSingleRatio: number
  changes: AccountChange[]
  settlements?: AccountSettlement[]
}

export interface AccountsDoc {
  schemaVersion: number
  accounts: TradingAccount[]
}

export type PortfolioHealth = 'normal' | 'attention' | 'critical'

export interface PortfolioPosition {
  tradeId: string
  status?: TradeStatus
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
  settledRealizedPnl?: number
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

export interface PortfolioRiskPosition {
  symbol: string
  weight: number
  annualizedVolatility: number | null
  riskContribution: number | null
}

export interface PortfolioRiskSnapshot {
  status: 'ok' | 'no_positions' | 'insufficient_data'
  lookbackDays: number
  source: 'canonical_kline_daily'
  methodology: string
  degraded: boolean
  dataAsOf: string | null
  observations: number
  metrics: {
    annualizedVolatility: number | null
    maxDrawdown: number | null
    maxPairCorrelation: number | null
    effectivePositions: number | null
    topWeight: number | null
  }
  positions: PortfolioRiskPosition[]
  correlation: { symbols: string[]; matrix: Array<Array<number | null>> }
  meta: { kept: string[]; dropped: string[]; warnings: string[] }
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

export interface PlanCheckContinuityMeta {
  mode: 'fresh' | 'incremental' | 'full_reanalysis'
  parent_attempt_id: string | null
  parent_artifact_id: string | null
  reason: string
  bars_delta: number
  new_bar_dates: string[]
  parent_data_as_of: string | null
  self_data_as_of: string | null
  compatibility: Record<string, boolean>
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
  continuity?: PlanCheckContinuityMeta
}

export interface PlanCheckArtifact {
  id?: string
  attempt_id: string
  request_id: string
  purpose?: string
  status: 'ok' | 'failed' | 'cancelled'
  data_as_of?: string | null
  symbol?: string | null
  parent_attempt_id?: string | null
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
  parent_attempt_id?: string | null
  continuity_mode?: PlanCheckContinuityMeta['mode'] | null
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

export function tradingGetPortfolioRisk(lookbackDays = 120) {
  return request<PortfolioRiskSnapshot>(
    `/api/trading/portfolio/risk?lookback_days=${encodeURIComponent(String(lookbackDays))}`,
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
  continuity = false,
): AsyncGenerator<PlanCheckStreamEvent> {
  const params = new URLSearchParams()
  if (profileId) params.set('profile_id', profileId)
  if (continuity) params.set('continuity', 'true')
  const qs = params.toString()
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

export interface PlanCheckContinuityChainNode {
  attempt_id: string
  artifact_id: string
  status: string
  symbol: string | null
  data_as_of: string | null
  created_at: string | null
  parent_attempt_id: string | null
  continuity_mode: PlanCheckContinuityMeta['mode'] | 'unknown'
  continuity_reason: string
  bars_delta: number
  usage: AiUsageMeta
}

export function tradingGetPlanCheckContinuity(attemptId: string) {
  return request<{
    chain: PlanCheckContinuityChainNode[]
    depth: number
    has_parent: boolean
  }>(`/api/trading/plan-checks/${encodeURIComponent(attemptId)}/continuity`)
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
