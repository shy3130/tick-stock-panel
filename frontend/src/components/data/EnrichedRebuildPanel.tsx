import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { usePreferences } from '@/lib/useSharedQueries'

export function EnrichedRebuildPanel({
  isRunning,
  earliestDate,
  onStart,
}: {
  isRunning: boolean
  earliestDate: string | null
  onStart: () => void
}) {
  const qc = useQueryClient()
  const prefs = usePreferences()
  const batchSize = prefs.data?.enriched_batch_size ?? 1000
  const [editing, setEditing] = useState(false)
  const [draftSize, setDraftSize] = useState(String(batchSize))
  const [hint, setHint] = useState<string | null>(null)
  const [repairStart, setRepairStart] = useState('')
  const [repairEnd, setRepairEnd] = useState('')

  function clampAndSave(raw: number) {
    if (isNaN(raw) || 1 > raw) { setHint('已自动设为最小值 1'); saveBatch.mutate(1); return }
    if (raw > 10000) { setHint('已自动设为上限 10000'); saveBatch.mutate(10000); return }
    setHint(null); saveBatch.mutate(raw)
  }

  const saveBatch = useMutation({
    mutationFn: (size: number) => api.updateEnrichedBatchSize(size),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      setEditing(false)
    },
  })
  const rebuild = useMutation({
    mutationFn: api.rebuildEnriched,
    onSuccess: () => {
      onStart()
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })

  const repair = useMutation({
    mutationFn: ({ startDate, endDate }: { startDate: string; endDate: string }) =>
      api.repairEnrichedRange(startDate, endDate),
    onSuccess: () => {
      onStart()
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
  })
  const today = new Date().toISOString().slice(0, 10)
  const repairDays = repairStart && repairEnd
    ? Math.floor((Date.parse(`${repairEnd}T00:00:00Z`) - Date.parse(`${repairStart}T00:00:00Z`)) / 86_400_000) + 1
    : null
  const invalidRange = repairDays !== null && (repairDays < 1 || repairDays > 31)
  const repairError = repair.error instanceof Error ? repair.error.message : null

  function handleRepair() {
    if (!repairStart || !repairEnd || invalidRange) return
    repair.mutate({ startDate: repairStart, endDate: repairEnd })
  }

  return (
    <div className="px-4 pb-4 pt-3 border-t border-accent/20 space-y-4">
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs font-medium text-foreground">批次大小</div>
            <div className="text-[10px] text-muted">每批计算的标的数量，影响内存占用与进度粒度</div>
          </div>
          {editing ? (
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                value={draftSize}
                onChange={e => setDraftSize(e.target.value)}
                className="w-20 px-2 py-1 text-xs font-mono rounded-btn border border-border bg-surface text-foreground text-right tabular-nums focus:outline-none focus:border-accent"
                min={1}
                max={10000}
                autoFocus
                onKeyDown={e => {
                  if (e.key === 'Enter') clampAndSave(parseInt(draftSize))
                  if (e.key === 'Escape') { setEditing(false); setHint(null) }
                }}
              />
              <button
                onClick={() => clampAndSave(parseInt(draftSize))}
                disabled={saveBatch.isPending}
                className="px-2 py-1 text-[10px] rounded-btn bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-50 transition-colors"
              >
                {saveBatch.isPending ? '…' : '保存'}
              </button>
              <button
                onClick={() => setEditing(false)}
                className="px-2 py-1 text-[10px] rounded-btn bg-elevated text-muted hover:text-foreground transition-colors"
              >
                取消
              </button>
            </div>
          ) : (
            <button
              onClick={() => { setDraftSize(String(batchSize)); setEditing(true) }}
              className="px-2.5 py-1 rounded-btn border border-border bg-surface text-xs font-mono text-foreground hover:border-accent/50 transition-colors tabular-nums"
            >
              {batchSize} 只/批
            </button>
          )}
        </div>
        <div className="flex items-start gap-1.5 px-3 py-1.5 rounded-btn bg-warning/10 border border-warning/20">
          <span className="text-[10px] text-warning leading-relaxed">
            每批内存占用 = 批次大小 × 日K历史天数。批次越大或日K历史越长，内存占用越高，可能导致程序崩溃。内存不足时请适当降低此值。
          </span>
        </div>
        {hint && (
          <div className="px-3 py-1 rounded-btn bg-accent/10 border border-accent/20 text-[10px] text-accent">
            {hint}
          </div>
        )}
      </div>

      <div className="rounded-card border border-accent/25 bg-accent/5 p-3 space-y-3">
        <div>
          <div className="text-xs font-medium text-foreground">历史缺口补算</div>
          <p className="mt-0.5 text-[10px] leading-relaxed text-secondary">
            从当前 DuckDB 数据源重新读取指定范围的 A 股日 K，仅原子覆盖该范围的 enriched 分区；不会写入 stock raw mirror。
          </p>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <label className="space-y-1">
            <span className="block text-[10px] text-muted">开始日期</span>
            <DatePicker
              value={repairStart}
              onChange={setRepairStart}
              min={earliestDate ?? undefined}
              max={repairEnd || today}
              placeholder="选择开始"
              align="left"
              buttonClassName="w-full"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-[10px] text-muted">结束日期</span>
            <DatePicker
              value={repairEnd}
              onChange={setRepairEnd}
              min={repairStart || earliestDate || undefined}
              max={today}
              placeholder="选择结束"
              align="right"
              buttonClassName="w-full"
            />
          </label>
        </div>
        <div className="flex items-center justify-between gap-2 text-[10px]">
          <span className={invalidRange ? 'text-warning' : 'text-muted'}>
            {repairDays === null ? '单次最多补算 31 个自然日' : `将补算 ${repairDays} 个自然日`}
          </span>
          {invalidRange && <span className="text-warning">范围需为 1–31 天</span>}
        </div>
        {repairError && (
          <div className="rounded-btn border border-danger/25 bg-danger/10 px-2 py-1 text-[10px] text-danger">
            {repairError}
          </div>
        )}
        <button
          onClick={handleRepair}
          disabled={isRunning || repair.isPending || !repairStart || !repairEnd || invalidRange}
          className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn border border-accent/35 bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
        >
          {repair.isPending ? (
            <><Loader2 className="h-3 w-3 animate-spin" />提交补算…</>
          ) : (
            <>按范围补算</>
          )}
        </button>
      </div>

      <div>
        <div className="text-[10px] text-muted mb-2">基于已有 kline_daily + adj_factor 全量计算前复权 + 技术指标 + 信号；fquant_local 的历史空洞请使用上方按范围补算。</div>
        <button
          onClick={() => rebuild.mutate()}
          disabled={isRunning || rebuild.isPending}
          className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 disabled:pointer-events-none transition-colors duration-150"
        >
          {rebuild.isPending ? (
            <><Loader2 className="h-3 w-3 animate-spin" />计算中…</>
          ) : (
            <>全量计算</>
          )}
        </button>
      </div>
    </div>
  )
}
