/**
 * 交易所口径异动监测页 — 只读展示 /api/abnormal/overview。
 *
 * 口径 (与后端 app/services/abnormal_moves.py 对齐):
 *   deviate_Nd = 最近 N 个连续交易日 Σ(个股日涨跌幅 - 基准指数日涨跌幅)
 *   阈值: 3日 主板20%/创业·科创30%/北交40%; 10日 +100%/-50%; 30日 +200%/-70%
 * 默认隐藏 ST (可切换); 基准缺失的板块显式不可用, 绝不显示伪零。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { TriangleAlert, ShieldAlert, RefreshCw } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { api } from '@/lib/api'
import type { AbnormalRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { boardTag } from '@/components/stock-table/primitives'

const STATUS_TABS = [
  { key: undefined, label: '全部' },
  { key: 'triggered', label: '已触发' },
  { key: 'edge', label: '接近' },
  { key: 'watch', label: '观察' },
  { key: 'normal', label: '正常' },
] as const

const BOARDS = ['主板', '创业板', '科创板', '北交所'] as const

const STATUS_STYLE: Record<string, string> = {
  triggered: 'bg-danger/15 text-danger border-danger/30',
  edge: 'bg-warning/15 text-warning border-warning/30',
  watch: 'bg-accent/10 text-accent border-accent/25',
  normal: 'bg-elevated text-muted border-border',
}

export function AbnormalMoves() {
  const [status, setStatus] = useState<AbnormalRow['status'] | undefined>('triggered')
  const [board, setBoard] = useState<string | undefined>(undefined)
  const [direction, setDirection] = useState<'up' | 'down' | undefined>(undefined)
  const [hideSt, setHideSt] = useState(true)

  const filterKey = `${status ?? ''}|${board ?? ''}|${direction ?? ''}|${hideSt}`
  const q = useQuery({
    queryKey: QK.abnormalOverview(filterKey),
    queryFn: () => api.abnormalOverview({ status, board, direction, hide_st: hideSt }),
    refetchInterval: 30000,
  })

  const rows = q.data?.rows ?? []
  const warnings = q.data?.warnings ?? []
  const total = q.data?.total ?? rows.length

  return (
    <div className="workspace-page h-full">
      <PageHeader
        title="异动监测"
        subtitle="交易所口径近似监测 · 非交易所公告 · 仅基于本地历史与指数数据"
      />
      <div className="workspace-content min-h-0 flex-1 overflow-hidden p-3 sm:p-4">
        <div className="mx-auto flex h-full min-h-0 w-full max-w-7xl flex-col gap-3">
          {/* 过滤栏 */}
          <div className="panel shrink-0 px-3 py-2">
            <div className="flex flex-wrap items-center gap-3">
              <div className="workspace-toolbar">
                {STATUS_TABS.map(t => (
                  <button
                    key={t.label}
                    onClick={() => setStatus(t.key)}
                    className={cn('btn-ghost h-7 px-2 text-[10px]', status === t.key ? 'bg-accent/15 text-accent' : '')}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <select
                value={board ?? ''}
                onChange={e => setBoard(e.target.value || undefined)}
                className="h-7 rounded border border-border bg-base px-1.5 text-[11px] text-foreground"
              >
                <option value="">全部板块</option>
                {BOARDS.map(b => <option key={b} value={b}>{b}</option>)}
              </select>
              <select
                value={direction ?? ''}
                onChange={e => setDirection((e.target.value || undefined) as 'up' | 'down' | undefined)}
                className="h-7 rounded border border-border bg-base px-1.5 text-[11px] text-foreground"
              >
                <option value="">涨跌都看</option>
                <option value="up">上涨偏离</option>
                <option value="down">下跌偏离</option>
              </select>
              <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-secondary">
                <input
                  type="checkbox"
                  checked={hideSt}
                  onChange={e => setHideSt(e.target.checked)}
                  className="h-3 w-3 accent-accent"
                />
                隐藏 ST
              </label>
              <span className="ml-auto text-[10px] text-muted">{total > rows.length ? `展示 ${rows.length} / ${total} 条` : `${total} 条`}</span>
              <button onClick={() => q.refetch()} className="btn-ghost h-7 w-7 px-0" title="刷新">
                <RefreshCw className={cn('h-3.5 w-3.5', q.isFetching && 'animate-spin')} />
              </button>
            </div>
          </div>

          {/* 警告条 */}
          {(warnings.length > 0 || q.isError || total > rows.length) && (
            <div className="shrink-0 space-y-1 rounded-lg border border-warning/30 bg-warning/5 px-3 py-2">
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-warning">
                <TriangleAlert className="h-3.5 w-3.5" /> 数据提示 (fail-soft, 不伪造数据)
              </div>
              {q.isError && (
                <div className="text-[11px] text-warning/90">接口请求失败: {(q.error as Error).message}</div>
              )}
              {warnings.map((w, i) => (
                <div key={i} className="text-[11px] text-warning/90">{w}</div>
              ))}
              {total > rows.length && (
                <div className="text-[11px] text-warning/90">
                  结果较多，当前仅展示前 {rows.length} 条；请用状态、板块或方向继续筛选。
                </div>
              )}
            </div>
          )}

          {/* 表格 */}
          <div className="panel min-h-0 flex-1 overflow-auto">
            {rows.length === 0 ? (
              <EmptyState
                icon={ShieldAlert}
                title={q.isLoading ? '加载中…' : '当前过滤条件下无异动'}
                hint="偏离值 = 个股日涨跌幅 − 基准指数日涨跌幅 的逐日累计 (3/10/30 日窗口)。"
              />
            ) : (
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-surface/95 backdrop-blur">
                  <tr className="border-b border-border text-[10px] text-muted">
                    <th className="px-3 py-2 font-medium">标的</th>
                    <th className="px-2 py-2 font-medium">板块</th>
                    <th className="px-2 py-2 font-medium">窗口</th>
                    <th className="px-2 py-2 font-medium">方向</th>
                    <th className="px-2 py-2 text-right font-medium">偏离值</th>
                    <th className="px-2 py-2 text-right font-medium">阈值</th>
                    <th className="px-2 py-2 text-right font-medium">达成率</th>
                    <th className="px-2 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 font-medium">基准指数</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    const bt = boardTag(r.symbol)
                    return (
                      <tr key={`${r.symbol}-${r.window}-${i}`} className="border-b border-border/40 hover:bg-elevated/40">
                        <td className="px-3 py-1.5">
                          <span className="font-mono font-medium text-foreground">{r.symbol}</span>
                          {bt && (
                            <span className={cn('ml-1.5 inline-flex h-3.5 w-3.5 items-center justify-center rounded border text-[8px] font-bold leading-none', bt.color)}>
                              {bt.label}
                            </span>
                          )}
                          {r.is_st && <span className="ml-1.5 rounded bg-danger/15 px-1 text-[9px] font-medium text-danger">ST</span>}
                          <span className="ml-1.5 text-secondary">{r.name}</span>
                        </td>
                        <td className="px-2 py-1.5 text-secondary">{r.board}</td>
                        <td className="px-2 py-1.5 font-mono text-secondary">{r.window}</td>
                        <td className={cn('px-2 py-1.5 font-medium', r.direction === 'up' ? 'text-danger' : 'text-bear')}>
                          {r.direction === 'up' ? '↑ 上涨' : '↓ 下跌'}
                        </td>
                        <td className={cn('px-2 py-1.5 text-right font-mono font-medium', r.deviation_pct >= 0 ? 'text-danger' : 'text-bear')}>
                          {r.deviation_pct >= 0 ? '+' : ''}{r.deviation_pct.toFixed(1)}%
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">{r.threshold_pct.toFixed(0)}%</td>
                        <td className="px-2 py-1.5 text-right font-mono text-secondary">{(r.ratio * 100).toFixed(0)}%</td>
                        <td className="px-2 py-1.5">
                          <span className={cn('rounded border px-1.5 py-0.5 text-[9px] font-medium', STATUS_STYLE[r.status] ?? STATUS_STYLE.normal)}>
                            {r.status === 'triggered' ? '已触发' : r.status === 'edge' ? '接近' : r.status === 'watch' ? '观察' : '正常'}
                          </span>
                        </td>
                        <td className="px-3 py-1.5 font-mono text-[10px] text-muted">
                          {r.benchmark_symbol}
                          {!r.benchmark_available && <span className="ml-1 text-warning">(不可用)</span>}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>

          <p className="shrink-0 text-[10px] leading-4 text-muted">
            规则近似: 3日 主板±20% / 创业板·科创板±30% / 北交所±40%; 10日 +100%/−50%; 30日 +200%/−70% (边界含等号)。
            北交所基准 899050 本地缺失时该板块显式不可用。本页为交易所规则近似监测, 非交易所公告。
          </p>
        </div>
      </div>
    </div>
  )
}
