/**
 * 分钟数据缺口体检面板 (数据页 · 分钟 K 卡片 → 设置弹窗内)。
 *
 * 发现缺口后可按单只或当前筛选结果批量补拉, 不承诺所有缺口均可补齐:
 * 上游分钟源通常只提供最近 N 天, 超出窗口的历史缺口补不回来 —— 盲目重试会
 * 白等几十秒且返回空。因此这里把缺口如实列出、明确标注可尝试补齐, 让用户
 * 知道「同步完成」不等于「数据完整」。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Download, Loader2, RefreshCw, Search } from 'lucide-react'
import { api, type MinuteGapRow } from '@/lib/api'
import { QK, MINUTE_DATA_QUERY_PREFIXES } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import './MinuteGapPanel.css'

const PAGE = 10

export function MinuteGapPanel({ hasCap = true, onJobStart }: { hasCap?: boolean; onJobStart?: (jobId: string) => void }) {
  const qc = useQueryClient()
  const [filter, setFilter] = useState('')
  const [page, setPage] = useState(0)
  const [busySymbol, setBusySymbol] = useState<string | null>(null)

  const gaps = useQuery({
    queryKey: QK.minuteGaps,
    queryFn: () => api.minuteGaps({ limit: 2000 }),
    staleTime: 30_000,
  })

  const repair = useMutation({
    mutationFn: (symbol: string) => api.syncMinuteSingle(symbol, 10),
    onSuccess: (r, symbol) => {
      const rows = (r as { rows?: number })?.rows ?? 0
      toast(rows > 0 ? `${symbol} 已拉取 ${rows} 行分钟数据` : `${symbol} 未拉到新数据（源可能不提供该历史窗口）`, rows > 0 ? 'success' : 'error')
      qc.invalidateQueries({ predicate: q => MINUTE_DATA_QUERY_PREFIXES.has(String(q.queryKey[0])) })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
    onError: (e: unknown) => toast(`补齐失败: ${String((e as Error)?.message ?? e)}`, 'error'),
    onSettled: () => setBusySymbol(null),
  })

  const all = gaps.data?.gaps ?? []
  const filtered = useMemo(() => {
    const kw = filter.trim().toUpperCase()
    return kw ? all.filter(g => g.symbol.toUpperCase().includes(kw) || g.date.includes(kw)) : all
  }, [all, filter])
  const repairSymbols = useMemo(() => [...new Set(filtered.map(g => g.symbol))], [filtered])
  const batchRepair = useMutation({
    mutationFn: (symbols: string[]) => api.repairMinuteGaps(symbols),
    onSuccess: (result, symbols) => {
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      toast(`已启动 ${symbols.length} 只标的的缺口补拉，可在数据页查看进度`, 'success')
      onJobStart?.(result.job_id)
    },
  })
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE))
  const currentPage = Math.min(page, pageCount - 1)
  const pageRows = filtered.slice(currentPage * PAGE, (currentPage + 1) * PAGE)

  if (gaps.isLoading) {
    return (
      <div className="flex h-24 items-center justify-center gap-2 text-xs text-muted">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        正在扫描分钟分区…
      </div>
    )
  }
  if (gaps.isError) {
    return (
      <div className="rounded-lg border border-danger/30 bg-danger/5 p-3 text-xs text-danger">
        体检失败：{String((gaps.error as Error)?.message ?? gaps.error)}
      </div>
    )
  }

  const summary = gaps.data?.summary_by_date ?? []
  const totalMissing = summary.reduce((n, c) => n + c.missing_bars, 0)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 text-[11px]">
        <div className="space-y-1.5">
          <span className="text-muted">
            扫描 {gaps.data?.scanned_dates ?? 0} 个交易日 · {gaps.data?.scanned_symbols ?? 0} 只标的
          </span>
          {all.length === 0 ? (
            <span className="flex items-center gap-1 text-bear">
              <CheckCircle2 className="h-3.5 w-3.5" />
              未发现分钟数据缺口
            </span>
          ) : (
            <span className="flex items-center gap-1 text-warning">
              <AlertTriangle className="h-3.5 w-3.5" />
              {all.length} 条缺口 · 合计缺 {totalMissing.toLocaleString()} 根
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {all.length > 0 && (
            <button
              type="button"
              onClick={() => batchRepair.mutate(repairSymbols)}
              disabled={!hasCap || repairSymbols.length === 0 || busySymbol !== null || batchRepair.isPending || gaps.isFetching}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-btn bg-accent/90 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-accent disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
            >
              {batchRepair.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
              {batchRepair.isPending ? '正在启动…' : `一键补齐（${repairSymbols.length} 只）`}
            </button>
          )}
          <button
            type="button"
            onClick={() => gaps.refetch()}
            disabled={gaps.isFetching || batchRepair.isPending || busySymbol !== null}
            className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-border px-2.5 py-1.5 text-[11px] text-secondary hover:text-accent disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
          >
            <RefreshCw className={`h-3 w-3 ${gaps.isFetching ? 'animate-spin' : ''}`} />
            重新体检
          </button>
        </div>
      </div>

      {(gaps.data?.inactive_symbol_days ?? 0) > 0 && (
        <p className="rounded-btn border border-border/60 bg-base/30 px-3 py-2 text-[11px] leading-relaxed text-muted">
          另有 {gaps.data!.inactive_symbol_days} 组整日零成交记录（日线核对无成交，可能停牌），未计入缺口，无需补拉。
        </p>
      )}

      {batchRepair.isError && (
        <div role="alert" className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
          批量补拉启动失败：{String((batchRepair.error as Error)?.message ?? batchRepair.error)}
        </div>
      )}
      {all.length > 0 && (
        <p className="text-[11px] leading-relaxed text-muted">
          一键补齐处理{filter.trim() ? '当前筛选的' : '已列出的全部'} {filtered.length} 条缺口，跨页按 {repairSymbols.length} 只标的去重补拉；任务结束后更新体检结果。
          {gaps.data?.truncated && <span className="text-warning"> 本次体检列表已截断，仅处理已列出的记录。</span>}
        </p>
      )}

      {summary.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {summary.map(c => (
            <span key={c.date} className="rounded bg-warning/10 px-2 py-0.5 text-[10px] text-warning">
              {c.date} · {c.gap_symbols} 只 · 缺 {c.missing_bars} 根
            </span>
          ))}
        </div>
      )}

      {all.length > 0 && (
        <>
          <div className="flex items-center gap-2 rounded-btn border border-border bg-base/30 px-3 focus-within:border-accent">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted" />
            <input
              value={filter}
              onChange={e => { setFilter(e.target.value); setPage(0) }}
              placeholder="按代码 / 日期过滤"
              aria-label="按代码或日期过滤缺口"
              className="h-9 min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none"
            />
          </div>

          {filtered.length === 0 ? (
            <div role="status" className="rounded-card border border-dashed border-border px-4 py-8 text-center text-xs text-muted">
              没有匹配的缺口记录，请尝试其他代码或日期。
            </div>
          ) : (
          <div className="minute-gap-list rounded-card border border-border/60">
            <div aria-hidden="true" className="minute-gap-columns rounded-t-card border-b border-border bg-base/50 px-3 py-2 text-[10px] font-medium text-muted">
              <span>代码</span><span>日期</span>
              <span className="text-right">实有 / 应有</span><span className="text-right">缺失</span>
              <span>数据范围</span><span className="text-right">操作</span>
            </div>
            <ul aria-label="分钟数据缺口" className="divide-y divide-border/40">
              {pageRows.map((g: MinuteGapRow) => (
                <li
                  key={`${g.symbol}-${g.date}`}
                  className="minute-gap-row px-3 py-2.5 text-[11px] hover:bg-elevated/40"
                >
                  <div className="minute-gap-identity">
                    <span className="break-all font-mono text-xs text-foreground">{g.symbol}</span>
                    <span className="whitespace-nowrap font-mono text-secondary">{g.date}</span>
                  </div>
                  <dl className="minute-gap-metrics">
                    <div className="minute-gap-metric">
                      <dt className="minute-gap-label text-[10px] text-muted">实有 / 应有</dt>
                      <dd className="whitespace-nowrap font-mono text-warning">{g.actual_bars}/{g.expected_bars}</dd>
                    </div>
                    <div className="minute-gap-metric">
                      <dt className="minute-gap-label text-[10px] text-muted">缺失</dt>
                      <dd className="font-mono text-warning">{g.missing_bars} 根</dd>
                    </div>
                    <div className="minute-gap-metric minute-gap-range">
                      <dt className="minute-gap-label text-[10px] text-muted">数据范围</dt>
                      <dd className="whitespace-nowrap font-mono text-secondary">{g.first_time ?? '—'} ~ {g.last_time ?? '—'}</dd>
                    </div>
                  </dl>
                  <div className="minute-gap-repair">
                    <button
                      type="button"
                      disabled={!hasCap || busySymbol !== null || batchRepair.isPending}
                      onClick={() => { setBusySymbol(g.symbol); repair.mutate(g.symbol) }}
                      className="whitespace-nowrap rounded-btn border border-border px-2 py-1.5 text-[10px] text-secondary hover:border-accent hover:text-accent disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                    >
                      {busySymbol === g.symbol ? '补齐中…' : '尝试补齐'}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
          )}

          {filtered.length > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-3 text-[11px] text-muted">
              <span>第 {currentPage * PAGE + 1}–{Math.min((currentPage + 1) * PAGE, filtered.length)} 条，共 {filtered.length} 条</span>
              {pageCount > 1 && (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setPage(Math.max(0, currentPage - 1))}
                    disabled={currentPage === 0}
                    className="rounded-btn border border-border px-2 py-1 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                  >
                    上一页
                  </button>
                  <span>{currentPage + 1} / {pageCount}</span>
                  <button
                    type="button"
                    onClick={() => setPage(Math.min(pageCount - 1, currentPage + 1))}
                    disabled={currentPage >= pageCount - 1}
                    className="rounded-btn border border-border px-2 py-1 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
                  >
                    下一页
                  </button>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {!hasCap && <p className="text-[11px] text-muted">当前分钟数据源不可用，可查看本地缺口；配置可用数据源后可尝试补齐。</p>}
      <p className="border-t border-border/60 pt-3 text-[11px] leading-relaxed text-muted">
        补齐会向当前分钟数据源重新拉取标的最近 10 个交易日窗口的分钟 K，批量操作沿用同一规则。
        分钟源通常只提供最近一段时间的分钟数据，<span className="text-warning">超出窗口的历史缺口无法补齐</span>，
        此时应视为永久残缺 —— 不影响已有数据的查看。
      </p>
    </div>
  )
}
