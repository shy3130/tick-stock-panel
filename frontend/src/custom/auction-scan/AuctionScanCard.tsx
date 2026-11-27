import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Calendar, Loader2, Play, Power, Radar, RefreshCw,
} from 'lucide-react'
import { api, type ExtDataConfig } from '@/lib/api'
import { fmtPct, fmtPrice, fmtVolume, priceColorClass } from '@/lib/format'
import { toNavItems, type NavItem } from '@/lib/listNav'
import { boardTag } from '@/components/stock-table/primitives'

export const AUCTION_SCAN_CONFIG_ID = 'ext_auction_scan'
const AUCTION_SCAN_COLLECTOR_ID = 'auction_scan.market_925'

interface AuctionScanRow {
  symbol: string
  trade_date?: string
  name?: string | null
  open_gap_pct?: number | null
  auction_vol_ratio?: number | null
  auction_price?: number | null
  prev_close?: number | null
  auction_volume?: number | null
  collected_at?: string | null
}

function fmtTimeOnly(iso: string | null | undefined): string {
  if (!iso) return '—'
  const match = iso.match(/(\d{2}:\d{2}:\d{2})/)
  return match ? match[1] : iso.slice(0, 19)
}

function fmtNextRun(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function toNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

export function AuctionScanCard({
  onOpenStock,
}: {
  onOpenStock: (symbol: string, name?: string, navList?: NavItem[]) => void
}) {
  const qc = useQueryClient()
  const [selectedDate, setSelectedDate] = useState<string>('')
  const [actionError, setActionError] = useState<string | null>(null)

  const listQuery = useQuery({
    queryKey: ['auction-scan', 'config'],
    queryFn: async () => {
      const res = await api.extDataList()
      return res.items.find(item => item.id === AUCTION_SCAN_CONFIG_ID) ?? null
    },
    staleTime: 15_000,
    refetchInterval: query => (query.state.data?.running ? 2_000 : false),
  })

  const config: ExtDataConfig | null = listQuery.data ?? null
  const pull = config?.pull ?? null
  const enabled = Boolean(pull?.enabled)
  const hasHistory = Boolean(config?.date_range && config.date_range.length > 0)

  const rowsQuery = useQuery({
    queryKey: ['auction-scan', 'rows', selectedDate || 'latest', pull?.last_run ?? ''],
    queryFn: () =>
      api.extDataRows(AUCTION_SCAN_CONFIG_ID, {
        date: selectedDate || undefined,
        limit: 500,
      }),
    enabled: Boolean(config) && (enabled || hasHistory),
    staleTime: 30_000,
  })

  const toggleMutation = useMutation({
    mutationFn: async (nextEnabled: boolean) => {
      const current = pull
      return api.extDataPullConfig(AUCTION_SCAN_CONFIG_ID, {
        url: current?.url ?? '',
        collector: current?.collector ?? AUCTION_SCAN_COLLECTOR_ID,
        method: current?.method ?? 'GET',
        schedule_minutes: current?.schedule_minutes ?? 1440,
        schedule_time: current?.schedule_time ?? '09:25',
        schedule_period: current?.schedule_period ?? 'daily',
        schedule_day: current?.schedule_day ?? 1,
        schedule_trading_only: current?.schedule_trading_only ?? true,
        time_window_start: current?.time_window_start ?? '09:25',
        time_window_end: current?.time_window_end ?? '09:30',
        timeout_seconds: current?.timeout_seconds ?? 60,
        enabled: nextEnabled,
      })
    },
    onSuccess: () => {
      setActionError(null)
      void qc.invalidateQueries({ queryKey: ['auction-scan'] })
    },
    onError: (err: Error) => {
      setActionError(err.message || '更新采集任务开关失败')
    },
  })

  const runMutation = useMutation({
    mutationFn: () => api.extDataPullRun(AUCTION_SCAN_CONFIG_ID, pull?.timeout_seconds ?? 60),
    onSuccess: () => {
      setActionError(null)
      setSelectedDate('')
      void qc.invalidateQueries({ queryKey: ['auction-scan'] })
    },
    onError: (err: Error) => {
      setActionError(err.message || '竞价扫描执行失败')
      void qc.invalidateQueries({ queryKey: ['auction-scan', 'config'] })
    },
  })

  const isCollecting = Boolean(config?.running) || runMutation.isPending
  const rowsData = rowsQuery.data
  const availableDates = rowsData?.dates ?? []
  const activeDate = rowsData?.date ?? (selectedDate || null)

  const rows: AuctionScanRow[] = useMemo(() => {
    const raw = rowsData?.rows ?? []
    return raw.map(r => ({
      symbol: String(r.symbol ?? ''),
      trade_date: r.trade_date ? String(r.trade_date) : undefined,
      name: r.name != null ? String(r.name) : null,
      open_gap_pct: toNumber(r.open_gap_pct),
      auction_vol_ratio: toNumber(r.auction_vol_ratio),
      auction_price: toNumber(r.auction_price),
      prev_close: toNumber(r.prev_close),
      auction_volume: toNumber(r.auction_volume),
      collected_at: r.collected_at != null ? String(r.collected_at) : null,
    }))
  }, [rowsData])

  const navItems = useMemo(
    () => toNavItems(rows.map(r => ({ symbol: r.symbol, name: r.name }))),
    [rows],
  )

  if (listQuery.isLoading) {
    return (
      <div className="flex items-center gap-3 rounded-card border border-border bg-surface/80 px-4 py-3">
        <span className="grid h-8 w-8 shrink-0 animate-pulse place-items-center rounded bg-elevated">
          <Radar className="h-4 w-4 text-muted/50" />
        </span>
        <div className="flex items-center gap-2">
          <span className="h-3 w-24 animate-pulse rounded-full bg-elevated/80" />
          <span className="h-3 w-32 animate-pulse rounded-full bg-elevated/50" />
        </div>
      </div>
    )
  }

  if (listQuery.isError) {
    return (
      <div className="rounded-card border border-danger/30 bg-danger/5 px-4 py-3">
        <div className="flex items-center gap-2 text-xs text-danger">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>全市场竞价扫描配置加载失败：{listQuery.error.message}</span>
          <button
            type="button"
            onClick={() => void listQuery.refetch()}
            className="ml-auto text-[11px] text-accent hover:underline"
          >
            重试
          </button>
        </div>
      </div>
    )
  }

  if (!config) {
    return (
      <div className="rounded-card border border-dashed border-border bg-surface/50 px-4 py-4">
        <div className="flex items-start gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded bg-elevated/60">
            <Radar className="h-4 w-4 text-muted/50" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-foreground">全市场竞价扫描</span>
              <span className="rounded-full border border-border bg-elevated px-2 py-px text-[9px] leading-tight text-muted">
                扩展采集未就绪
              </span>
            </div>
            <p className="mt-1 text-[11px] text-muted">
              未检测到扩展数据表 {AUCTION_SCAN_CONFIG_ID}，请确认后端竞价扫描扩展已加载。
            </p>
          </div>
        </div>
      </div>
    )
  }

  const lastStatus = pull?.last_status ?? null
  const lastMessage = pull?.last_message ?? null
  const nextRunText = fmtNextRun(pull?.next_run)

  return (
    <div className="rounded-card border border-border bg-surface/80">
      {/* 顶部标题与控制栏 */}
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <span
          className={`grid h-8 w-8 shrink-0 place-items-center rounded ${
            enabled
              ? 'bg-accent/15 text-accent ring-1 ring-accent/25'
              : 'bg-elevated/60 text-muted'
          }`}
        >
          {isCollecting ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Radar className="h-4 w-4" />
          )}
        </span>

        <div className="min-w-0 flex-1 leading-tight">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-semibold text-foreground">全市场竞价扫描</span>
            {isCollecting ? (
              <span className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/10 px-2 py-px text-[10px] text-accent">
                <Loader2 className="h-2.5 w-2.5 animate-spin" />
                采集中
              </span>
            ) : enabled ? (
              <span className="rounded-full border border-bull/30 bg-bull/10 px-2 py-px text-[10px] text-bull">
                已启用 · 交易日 09:25
              </span>
            ) : (
              <span className="rounded-full border border-border bg-elevated px-2 py-px text-[10px] text-muted">
                待采集任务启用
              </span>
            )}
            {availableDates.length > 0 && (
              <span className="text-[10px] text-muted">
                已累计 {availableDates.length} 个交易日
              </span>
            )}
          </div>
          <div className="mt-0.5 text-[10px] text-muted">
            高开 ≥5% 且竞价量比 ≥10 · 仅从启用后首个成功 9:25 扫描日起按日积累，不补录历史
            {enabled && nextRunText && !isCollecting && ` · 下次预计 ${nextRunText}`}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {availableDates.length > 0 && (
            <label className="inline-flex items-center gap-1 rounded border border-border bg-base px-2 py-1 text-[11px] text-secondary">
              <Calendar className="h-3 w-3 text-muted" />
              <select
                aria-label="选择历史交易日"
                value={selectedDate || activeDate || ''}
                onChange={e => setSelectedDate(e.target.value)}
                className="bg-transparent text-[11px] text-foreground outline-none"
              >
                {availableDates.map(d => (
                  <option key={d} value={d} className="bg-surface text-foreground">
                    {d}
                  </option>
                ))}
              </select>
            </label>
          )}

          {enabled && (
            <button
              type="button"
              disabled={isCollecting}
              onClick={() => runMutation.mutate()}
              title="立即执行一次竞价快照扫描"
              className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
            >
              <Play className="h-3 w-3" />
              立即扫描
            </button>
          )}

          <button
            type="button"
            disabled={toggleMutation.isPending}
            onClick={() => toggleMutation.mutate(!enabled)}
            className={`inline-flex h-7 items-center gap-1 rounded px-2.5 text-[11px] font-medium transition-colors disabled:opacity-50 ${
              enabled
                ? 'border border-border bg-base text-secondary hover:text-foreground'
                : 'bg-accent text-white hover:bg-accent/90'
            }`}
          >
            <Power className="h-3 w-3" />
            {enabled ? '关闭自动扫描' : '开启 9:25 自动扫描'}
          </button>
        </div>
      </div>

      {/* 异常/部分失败/跳过状态提示条 */}
      {(actionError || lastStatus === 'error' || lastStatus === 'partial') && (
        <div
          role="alert"
          className={`mx-4 mb-3 flex items-start gap-2 rounded border px-3 py-2 text-[11px] ${
            lastStatus === 'partial' && !actionError
              ? 'border-warning/30 bg-warning/10 text-warning'
              : 'border-danger/30 bg-danger/10 text-danger'
          }`}
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div className="min-w-0 flex-1">
            {actionError
              ? actionError
              : lastStatus === 'partial'
                ? `部分批次失败，已保留已成功结果：${lastMessage || '请检查数据源状态'}`
                : `最近一次扫描失败：${lastMessage || '请检查实时行情数据源配置'}`}
          </div>
        </div>
      )}

      {lastStatus === 'skipped' && lastMessage && !actionError && (
        <div className="mx-4 mb-3 rounded border border-border/60 bg-elevated/40 px-3 py-1.5 text-[11px] text-muted">
          最近一次调度已跳过：{lastMessage}
        </div>
      )}

      {/* 内容区：未启用 / 采集中 / 历史读取失败 / 无历史 / 无命中 / 命中列表 */}
      <div className="border-t border-border/60 px-4 py-3">
        {isCollecting ? (
          <div className="flex items-center gap-2 py-4 text-xs text-secondary">
            <Loader2 className="h-4 w-4 animate-spin text-accent" />
            <span>正在扫描全市场竞价快照并计算高开幅度与竞价量比…</span>
          </div>
        ) : !enabled && !hasHistory ? (
          <p className="py-1 text-[11px] leading-relaxed text-muted">
            尚未开启 9:25 全市场竞价扫描。开启后，系统将在每个交易日 09:25–09:30 竞价终态窗口内自动扫描全市场
            A 股，筛选高开 ≥5% 且竞价量比 ≥10 的标的并按交易日落盘；启用前不补录历史数据。
          </p>
        ) : rowsQuery.isError ? (
          <div className="flex items-center gap-2 py-2 text-xs text-danger">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>历史记录读取失败：{rowsQuery.error.message}</span>
            <button
              type="button"
              onClick={() => void rowsQuery.refetch()}
              className="ml-auto inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
            >
              <RefreshCw className="h-3 w-3" />
              重试
            </button>
          </div>
        ) : rowsQuery.isLoading ? (
          <div className="py-3 text-xs text-muted">正在加载历史竞价扫描结果…</div>
        ) : !activeDate ? (
          <p className="py-1 text-[11px] leading-relaxed text-muted">
            自动扫描已开启，尚无已落盘的交易日记录。首个交易日 09:25 扫描完成后，结果将在此按日期倒序展示并逐日积累。
          </p>
        ) : rows.length === 0 ? (
          <div className="py-2 text-xs text-muted">
            {activeDate} 竞价扫描已完成，当日无符合条件标的（高开 ≥5% 且竞价量比 ≥10）。
          </div>
        ) : (
          <div className="overflow-x-auto">
            <div className="mb-2 flex items-center justify-between text-[11px] text-muted">
              <span>
                交易日 <span className="font-medium text-foreground">{activeDate}</span> · 命中{' '}
                <span className="font-medium text-foreground">{rows.length}</span> 只
              </span>
              {rows[0]?.collected_at && (
                <span>采集时间 {fmtTimeOnly(rows[0].collected_at)}</span>
              )}
            </div>
            <table className="w-full border-collapse text-left text-xs tabular-nums">
              <thead>
                <tr className="border-b border-border/60 text-[10px] text-muted">
                  <th className="py-1.5 pr-3 font-medium">标的</th>
                  <th className="py-1.5 px-2 text-right font-medium">高开幅度</th>
                  <th className="py-1.5 px-2 text-right font-medium">竞价量比</th>
                  <th className="py-1.5 px-2 text-right font-medium">竞价价 / 昨收</th>
                  <th className="py-1.5 pl-2 text-right font-medium">竞价量(手)</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(row => {
                  const tag = boardTag(row.symbol)
                  return (
                    <tr
                      key={row.symbol}
                      onClick={() => onOpenStock(row.symbol, row.name || undefined, navItems)}
                      className="cursor-pointer border-b border-border/40 transition-colors last:border-b-0 hover:bg-elevated/50"
                    >
                      <td className="py-2 pr-3">
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium text-foreground">{row.name || row.symbol}</span>
                          <span className="text-[10px] text-muted">{row.symbol}</span>
                          {tag && (
                            <span className={`rounded border px-1 py-px text-[9px] leading-tight ${tag.color}`}>
                              {tag.label}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className={`py-2 px-2 text-right font-medium ${priceColorClass(row.open_gap_pct)}`}>
                        {fmtPct(row.open_gap_pct)}
                      </td>
                      <td className="py-2 px-2 text-right font-medium text-foreground">
                        {row.auction_vol_ratio != null ? `${row.auction_vol_ratio.toFixed(1)}x` : '—'}
                      </td>
                      <td className="py-2 px-2 text-right text-secondary">
                        {fmtPrice(row.auction_price)} / {fmtPrice(row.prev_close)}
                      </td>
                      <td className="py-2 pl-2 text-right text-secondary">
                        {fmtVolume(row.auction_volume)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
