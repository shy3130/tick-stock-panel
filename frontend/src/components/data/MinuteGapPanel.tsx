/**
 * 分钟数据缺口体检面板 (数据页 · 分钟 K 卡片 → 设置弹窗内)。
 *
 * 只做「发现」和「按需补拉单只」, 不批量承诺补齐:
 * 上游分钟源通常只提供最近 N 天, 超出窗口的历史缺口补不回来 —— 盲目重试会
 * 白等几十秒且返回空。因此这里把缺口如实列出、明确标注可尝试补齐, 让用户
 * 知道「同步完成」不等于「数据完整」。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, Search } from 'lucide-react'
import { api, type MinuteGapRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const PAGE = 50

export function MinuteGapPanel() {
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
      qc.invalidateQueries({ queryKey: QK.minuteGaps })
      qc.invalidateQueries({ queryKey: ['kline-minute'] })
    },
    onError: (e: unknown) => toast(`补齐失败: ${String((e as Error)?.message ?? e)}`, 'error'),
    onSettled: () => setBusySymbol(null),
  })

  const all = gaps.data?.gaps ?? []
  const filtered = useMemo(() => {
    const kw = filter.trim().toUpperCase()
    return kw ? all.filter(g => g.symbol.toUpperCase().includes(kw) || g.date.includes(kw)) : all
  }, [all, filter])
  const pageRows = filtered.slice(page * PAGE, page * PAGE + PAGE)
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE))

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
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
        <span className="text-muted">
          扫描 {gaps.data?.scanned_dates ?? 0} 个交易日 · {gaps.data?.scanned_symbols ?? 0} 只标的
        </span>
        {filtered.length === 0 ? (
          <span className="inline-flex items-center gap-1 text-emerald-400">
            <CheckCircle2 className="h-3.5 w-3.5" />
            未发现分钟数据缺口
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-warning">
            <AlertTriangle className="h-3.5 w-3.5" />
            {filtered.length} 条缺口 · 合计缺 {totalMissing.toLocaleString()} 根
          </span>
        )}
        <button
          type="button"
          onClick={() => gaps.refetch()}
          disabled={gaps.isFetching}
          className="ml-auto inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] text-secondary hover:text-accent disabled:opacity-50"
        >
          <RefreshCw className={`h-3 w-3 ${gaps.isFetching ? 'animate-spin' : ''}`} />
          重新体检
        </button>
      </div>

      {summary.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {summary.map(c => (
            <span key={c.date} className="rounded bg-warning/10 px-2 py-0.5 text-[10px] text-warning">
              {c.date} · {c.gap_symbols} 只 · 缺 {c.missing_bars} 根
            </span>
          ))}
        </div>
      )}

      {filtered.length > 0 && (
        <>
          <div className="flex items-center gap-1.5">
            <Search className="h-3.5 w-3.5 text-muted" />
            <input
              value={filter}
              onChange={e => { setFilter(e.target.value); setPage(0) }}
              placeholder="按代码 / 日期过滤"
              className="h-7 flex-1 rounded border border-border bg-surface px-2 text-[11px] text-foreground outline-none focus:border-accent"
            />
          </div>

          <div className="overflow-hidden rounded border border-border/60">
            <div className="grid grid-cols-[minmax(0,1.2fr)_90px_78px_78px_minmax(0,1fr)_74px] border-b border-border bg-base/50 px-2 py-1.5 text-[10px] font-medium text-muted">
              <span>代码</span>
              <span>日期</span>
              <span className="text-right">实有/应有</span>
              <span className="text-right">缺</span>
              <span>数据范围</span>
              <span className="text-right">操作</span>
            </div>
            <div className="max-h-72 overflow-y-auto">
              {pageRows.map((g: MinuteGapRow) => (
                <div
                  key={`${g.symbol}-${g.date}`}
                  className="grid grid-cols-[minmax(0,1.2fr)_90px_78px_78px_minmax(0,1fr)_74px] items-center border-b border-border/40 px-2 py-1 text-[11px] last:border-b-0 hover:bg-elevated/40"
                >
                  <span className="truncate font-mono text-foreground" title={g.symbol}>{g.symbol}</span>
                  <span className="font-mono text-secondary">{g.date}</span>
                  <span className="text-right font-mono text-warning">{g.actual_bars}/{g.expected_bars}</span>
                  <span className="text-right font-mono text-muted">-{g.missing_bars}</span>
                  <span className="truncate font-mono text-[10px] text-muted">
                    {g.first_time ?? '—'} ~ {g.last_time ?? '—'}
                  </span>
                  <span className="text-right">
                    <button
                      type="button"
                      disabled={busySymbol !== null}
                      onClick={() => { setBusySymbol(g.symbol); repair.mutate(g.symbol) }}
                      className="rounded border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:border-accent hover:text-accent disabled:opacity-50"
                    >
                      {busySymbol === g.symbol ? '补齐中…' : '尝试补齐'}
                    </button>
                  </span>
                </div>
              ))}
            </div>
          </div>

          {pageCount > 1 && (
            <div className="flex items-center justify-center gap-2 text-[11px] text-muted">
              <button
                type="button"
                onClick={() => setPage(p => Math.max(0, p - 1))}
                disabled={page === 0}
                className="rounded border border-border px-2 py-0.5 disabled:opacity-40"
              >
                上一页
              </button>
              <span>{page + 1} / {pageCount}</span>
              <button
                type="button"
                onClick={() => setPage(p => Math.min(pageCount - 1, p + 1))}
                disabled={page >= pageCount - 1}
                className="rounded border border-border px-2 py-0.5 disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}

      <p className="text-[10px] leading-relaxed text-muted">
        「尝试补齐」会向当前分钟数据源重新拉取该标的最近 10 个交易日的分钟 K。
        分钟源通常只提供最近一段时间的分钟数据，<span className="text-warning">超出窗口的历史缺口无法补齐</span>，
        此时应视为永久残缺 —— 不影响已有数据的查看。
      </p>
    </div>
  )
}
