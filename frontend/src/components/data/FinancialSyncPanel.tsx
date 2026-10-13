/**
 * 财务数据 · 手动拉取面板 (数据页「财务数据」画像卡的弹窗内容)。
 *
 * 财务有独立的 FinancialScheduler, 不在盘后管道里 —— 数据页顶部的「立即同步」
 * 不含财务, 此前想拉财务只能去财务分析页。本面板把那张页面的 5 张表状态与
 * 单表拉取按钮搬进画像卡, 让「数据从哪来 / 拉没拉」与其它数据集一致。
 *
 * 5 张表是 financial 这一个能力下的子表 (同一个 Key 提供、同一个门禁), 故此处
 * 按子项罗列而非拆成独立能力 —— 与设置页能力路由矩阵的粒度保持一致。
 */
import { useEffect, useState } from 'react'
import { Loader2, Download, CheckCircle2, Hourglass, Clock, Table2 } from 'lucide-react'
import { useFinancialStatus, useFinancialSync } from '@/lib/useFinancials'
import { fmtBigNum } from '@/lib/format'
import { toast } from '@/components/Toast'

const TABLE_LABELS: Record<string, string> = {
  metrics: '核心指标',
  income: '利润表',
  balance_sheet: '资产负债表',
  cash_flow: '现金流量表',
  shares: '股本表',
}
// 与后端 FINANCIAL_TABLES 同序 (核心指标 → 三张报表 → 股本)
const TABLE_ORDER = ['metrics', 'income', 'balance_sheet', 'cash_flow', 'shares'] as const

export function FinancialSyncPanel() {
  const { data: status, isLoading } = useFinancialStatus()
  const syncMut = useFinancialSync()

  // 本次同步的起止追踪: 后端每张表完成即更新 last_sync, 用时间戳判断进度,
  // 与财务分析页同一套口径 (那里是全页 5 张卡, 这里是紧凑列表)。
  const [syncStartedAt, setSyncStartedAt] = useState<number | null>(null)
  const [syncSingleTable, setSyncSingleTable] = useState<string | null>(null)

  const syncing = (status?.syncing ?? false) || syncMut.isPending
  const lastSync = status?.last_sync ?? {}
  const tables = status?.tables ?? {}

  // 同步自然结束 (服务端 syncing 由 true→false) 即清空本轮记录。
  // 不依赖 mutation 的 onSettled —— 它瞬间触发, 会误清。
  useEffect(() => {
    if (!syncing && syncStartedAt !== null) {
      setSyncStartedAt(null)
      setSyncSingleTable(null)
    }
  }, [syncing, syncStartedAt])

  const doneThisRound = (key: string): boolean => {
    if (!syncStartedAt || !syncing) return false
    if (syncSingleTable && key !== syncSingleTable) return false
    const ls = lastSync[key]
    return !!ls && new Date(ls).getTime() >= syncStartedAt
  }
  const isFullSync = syncing && syncStartedAt !== null && !syncSingleTable
  const currentSyncingTable = syncStartedAt !== null && syncing
    ? (syncSingleTable
        ? (doneThisRound(syncSingleTable) ? null : syncSingleTable)
        : TABLE_ORDER.find(t => !doneThisRound(t)) ?? null)
    : null
  const isWaiting = (key: string): boolean =>
    !!isFullSync && !doneThisRound(key) && currentSyncingTable !== key

  const syncedCount = TABLE_ORDER.filter(t => doneThisRound(t)).length

  const handleSync = (table: string) => {
    if (syncing) return
    setSyncStartedAt(Date.now())
    setSyncSingleTable(table === 'all' ? null : table)
    syncMut.mutate(table, {
      onSuccess: (r) => {
        if (!r.synced?.started) {
          if (r.synced?.reason === 'already running') {
            toast('财务数据正在同步中,请稍候', 'success')
          } else if (r.synced?.reason === 'no FINANCIAL capability') {
            toast('财务数据能力未就绪,请检查 API Key 或刷新页面后重试', 'error')
          } else {
            toast(`同步未能开始${r.synced?.reason ? `:${r.synced.reason}` : ''}`, 'error')
          }
          setSyncStartedAt(null)
          setSyncSingleTable(null)
        }
      },
      onError: () => {
        setSyncStartedAt(null)
        setSyncSingleTable(null)
      },
    })
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 py-10 text-xs text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> 读取财务数据状态…
      </div>
    )
  }

  if (!status?.available) {
    return (
      <div className="rounded-card border border-dashed border-border bg-base/30 px-4 py-6 text-center">
        <Table2 className="mx-auto h-6 w-6 text-muted" />
        <div className="mt-2 text-xs text-secondary">当前数据源不提供财务数据</div>
        <div className="mt-1 text-[11px] text-muted">在 设置 → 数据源 配置提供财务数据的数据源后可用</div>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="text-[11px] leading-relaxed text-muted">
        5 张表同属「财务数据」这一个能力 (同一数据源 / 同一门禁), 可整体同步或单独更新某一张。
        财务不在盘后管道内, 需在此手动拉取。
      </div>

      <div className="divide-y divide-border/50 rounded-card border border-border bg-base/20">
        {TABLE_ORDER.map(key => {
          const info = tables[key]
          const rows = info?.rows ?? 0
          const hasData = rows > 0
          const done = doneThisRound(key)
          const thisSyncing = currentSyncingTable === key
          const waiting = isWaiting(key)
          const lsTime = lastSync[key]
          return (
            <div key={key} className="flex items-center gap-3 px-3 py-2">
              <span className="flex w-4 shrink-0 justify-center">
                {done ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-bear" />
                ) : thisSyncing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />
                ) : waiting ? (
                  <Hourglass className="h-3.5 w-3.5 text-muted/50" />
                ) : (
                  <Table2 className={`h-3.5 w-3.5 ${hasData ? 'text-accent/70' : 'text-muted/50'}`} />
                )}
              </span>

              <span className="w-20 shrink-0 text-xs text-foreground">{TABLE_LABELS[key]}</span>

              <span className="min-w-0 flex-1 truncate text-[11px] text-muted">
                {hasData ? (
                  <>
                    <span className="font-mono tabular-nums text-secondary">{fmtBigNum(rows)}</span> 行
                    <span className="mx-1 text-border">·</span>
                    <span className="font-mono tabular-nums text-secondary">{fmtBigNum(info?.symbols ?? 0)}</span> 只标的
                  </>
                ) : (
                  '尚无数据'
                )}
              </span>

              <span className="hidden shrink-0 items-center gap-1 text-[10px] text-muted/80 sm:flex">
                <Clock className="h-2.5 w-2.5" />
                {lsTime
                  ? new Date(lsTime).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
                  : '未同步'}
              </span>

              <button
                onClick={() => handleSync(key)}
                disabled={syncing}
                title={syncing ? '正在同步…' : `仅更新${TABLE_LABELS[key]}`}
                aria-label={`仅更新${TABLE_LABELS[key]}`}
                className="shrink-0 rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-30"
              >
                {thisSyncing
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <Download className="h-3.5 w-3.5" />}
              </button>
            </div>
          )
        })}
      </div>

      <div className="flex items-center gap-2">
        {syncing && (
          <span className="flex min-w-0 items-center gap-1.5 text-[11px] text-accent/80">
            <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
            <span className="truncate">
              {isFullSync
                ? `已同步 ${syncedCount}/${TABLE_ORDER.length} 张表…`
                : `同步${syncSingleTable ? TABLE_LABELS[syncSingleTable] ?? syncSingleTable : ''}…`}
            </span>
          </span>
        )}
        <button
          onClick={() => handleSync('all')}
          disabled={syncing}
          className="ml-auto inline-flex h-7 shrink-0 items-center gap-1.5 rounded-btn border border-accent/30 bg-gradient-to-r from-accent/25 to-accent/10 px-3 text-xs font-medium text-accent transition-all duration-150 hover:from-accent/35 hover:to-accent/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {syncing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
          {syncing ? '同步中…' : '全部同步'}
        </button>
      </div>
    </div>
  )
}
