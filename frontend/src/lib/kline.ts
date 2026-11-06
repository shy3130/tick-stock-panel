/**
 * 日K查询配置 — klineDaily 的唯一权威 options。
 *
 * StockDailyKChart(图表) 与 StockPanel(信息条) 各自 useQuery 共享同一 cache key,
 * React Query 按 key 去重只发一次请求; 邻近预取 prefetchQuery 也复用本配置, 三处不会漂移。
 *
 * placeholderData 内置"仅同 symbol 占位"守卫: 改日期范围/扩展字段时旧数据可暂显(不闪),
 * 切股时不透传上一只股票的数据(不误显示)。
 */
import { api, type KlineDailyLatestResponse, type KlineDailyResponse } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 分时 tab 多日分时默认周期 (StockPanel 预取与弹窗存储回退共用, 避免魔数两处漂移) */
export const DEFAULT_INTRADAY_DAYS = 10

export function klineDailyQueryOptions(
  symbol: string,
  dateRange: { start: string; end: string },
  extColumns?: string,
) {
  return {
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, extColumns),
    queryFn: () => api.klineDaily(symbol, undefined, dateRange, extColumns),
    // 工厂无 TData 泛型, 参数用 any 以便 useQuery/prefetchQuery 共用
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === symbol ? prev : undefined
    },
  }
}

export function klineDailyLatestQueryOptions(symbol: string) {
  return {
    queryKey: QK.klineLatest(symbol),
    queryFn: () => api.klineDailyLatest(symbol),
  }
}

export function mergeLatestKlineRow(
  current: KlineDailyResponse | undefined,
  latest: KlineDailyLatestResponse,
): KlineDailyResponse | undefined {
  if (!current || current.symbol !== latest.symbol) return current
  if (latest.asset_type && latest.asset_type !== current.asset_type) current = { ...current, asset_type: latest.asset_type }
  if (latest.quote_freshness) current = { ...current, quote_freshness: latest.quote_freshness }
  if (!latest.row) return current

  const latestDate = String(latest.row.date).slice(0, 10)
  const last = current.rows.at(-1)
  if (!last) return { ...current, rows: [{ ...latest.row, date: latestDate }] }

  const lastDate = String(last.date).slice(0, 10)
  if (latestDate < lastDate) return current
  if (latestDate === lastDate) {
    return {
      ...current,
      rows: [...current.rows.slice(0, -1), { ...last, ...latest.row, date: latestDate }],
    }
  }
  return { ...current, rows: [...current.rows, { ...latest.row, date: latestDate }] }
}

/**
 * 单日分时查询配置 — 与 klineDailyQueryOptions 同风格的单源 options (date 为空 = 最新日内)。
 *
 * live 仅当日盘中生效: 传 true 时后端实时拉取最新K, 不被分钟增量落盘(≥60s 一轮)拖慢;
 * 历史日期后端自行忽略 live, 故多日图与预取恒传 true 亦不影响历史读取。
 */
export function klineMinuteQueryOptions(symbol: string, date?: string, live?: boolean) {
  return {
    queryKey: QK.klineMinute(symbol, date ?? ''),
    queryFn: () => api.klineMinute(symbol, date ?? undefined, live),
  }
}

/** 收盘分钟标签: 连续竞价全天最后一根分钟K的时间。 */
const CLOSE_MINUTE = '15:00'

/** 全天真正定版时刻: 15:00 收盘后仍可按收盘价成交 (盘后固定价) 至 15:30,
 * 其间成交量/额仍可能变化 —— 与后端 quote_service 定版重试窗口终点
 * (15:30) 同口径; 盘后管道默认 15:35 在此之后启动。 */
const MARKET_ALL_OVER = '15:30'

/** 北京时间今日 YYYY-MM-DD (与后端 cn_today 同口径; 不用 UTC toISOString, 避免傍晚错日)。 */
export function cnToday(now = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(now)
}

export function klineDateRange(months = 6, today = cnToday()): { start: string; end: string } {
  const [year, month, day] = today.split('-').map(Number)
  const start = new Date(Date.UTC(year, month - 1 - months, 1))
  const lastDay = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 0)).getUTCDate()
  start.setUTCDate(Math.min(day, lastDay))
  return { start: start.toISOString().slice(0, 10), end: today }
}

/** 北京时间此刻 'HH:MM'。 */
function cnNowHHMM(): string {
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date())
}

/** 行 datetime ('YYYY-MM-DD HH:MM:SS' 北京墙钟契约) → 'HH:MM'; 非常规格式返回 ''。 */
function rowMinute(r: { datetime?: string } | undefined): string {
  const s = String(r?.datetime ?? '')
  return s.length >= 16 ? s.slice(11, 16) : ''
}

/**
 * 分时轮询停表 — 数据不可变时停止, 消除无效请求 (周末/盘后/翻看历史日期)。
 *
 * - 未传间隔 → 不轮询 (调用方默认关闭)
 * - 无数据 (source=none) → 停
 * - 响应日期 < 北京今天 → 停: 历史日的本地分钟K不可变 (周末"最新"会被
 *   后端回退到上一交易日, 同样命中此规则)
 * - 当日: 收盘根 (≥15:00 的K) 已出现 **且** 已过 15:30 → 停。
 *   完整性**只认收盘根, 不按根数** —— 实测本系统全天 241 根 (首根 09:30
 *   竞价根 + 15:00 收盘根), 按根数阈值会在第 240 根 (14:59) 到位时误停,
 *   恰好错过 15:00 收盘根; 收盘根判据还天然兼容盘中缺根与部分交易日。
 *   而 15:00 收盘根出现后**不能立即停**: 盘后按收盘价成交可持续到 15:30,
 *   其间量/额仍可能更新 (当前数据源分钟K止于 15:00, 此窗口为防御性保留,
 *   与后端"15:30 才允许定版/重建"的边界一致)。
 * - 其余 (当日 15:30 前) → 按间隔继续
 */
export function minuteRefetchInterval(ms?: number) {
  return (query: any): number | false => {
    if (ms == null) return false
    const d = query.state.data
    if (!d) return ms
    if (d.source === 'none') return false
    if (typeof d.date === 'string' && d.date < cnToday()) return false
    const closeBarArrived = (d.rows ?? []).some((r: { datetime?: string }) => rowMinute(r) >= CLOSE_MINUTE)
    if (closeBarArrived && cnNowHHMM() >= MARKET_ALL_OVER) return false
    return ms
  }
}

/** 多日分时查询配置 — 分时 tab 的 StockMultiDayIntradayChart 与 邻近预取 共用。
 * 内嵌「仅同 symbol 占位」守卫 (key 结构 ['kline-minute-range', symbol, days], index 1 为 symbol),
 * 与 klineDailyQueryOptions 同源, 调用点不再各自手写。 */
export function klineMinuteRangeQueryOptions(symbol: string, days: number) {
  return {
    queryKey: QK.klineMinuteRange(symbol, days),
    queryFn: () => api.klineMinuteRange(symbol, days),
    placeholderData: (prev: any, prevQuery: any) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === symbol ? prev : undefined
    },
  }
}
