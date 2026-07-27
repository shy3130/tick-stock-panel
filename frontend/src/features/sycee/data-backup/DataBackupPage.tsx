import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArchiveRestore,
  DatabaseBackup,
  Download,
  FileJson,
  Loader2,
  RefreshCw,
  Upload,
} from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'
import { getNavIconMeta } from '@/lib/navRegistry'
import {
  DATA_BACKUP_QUERY_KEY,
  syceeDataBackupApi,
  type SyceeBackupDocument,
} from './api'
import {
  backupFilename,
  backupSummary,
  MAX_BACKUP_FILE_BYTES,
  parseBackupFile,
} from './backupFile'

interface SelectedBackup {
  name: string
  size: number
  document: SyceeBackupDocument
}

function downloadBackup(backup: SyceeBackupDocument) {
  const blob = new Blob(
    [JSON.stringify(backup, null, 2)],
    { type: 'application/json;charset=utf-8' },
  )
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = backupFilename(backup.exported_at)
  anchor.click()
  URL.revokeObjectURL(url)
}

function formatTime(value: string): string {
  const parsed = new Date(value)
  return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString('zh-CN') : value
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function DataBackupPage() {
  const navMeta = getNavIconMeta('/data-backup')
  const queryClient = useQueryClient()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [selected, setSelected] = useState<SelectedBackup | null>(null)
  const [confirmRestore, setConfirmRestore] = useState(false)
  const current = useQuery({
    queryKey: DATA_BACKUP_QUERY_KEY,
    queryFn: syceeDataBackupApi.read,
  })
  const exportData = useMutation({
    mutationFn: syceeDataBackupApi.read,
    onSuccess: backup => {
      downloadBackup(backup)
      toast('Sycee 数据备份已导出', 'success')
    },
    onError: error => toast(error instanceof Error ? error.message : '数据备份导出失败', 'error'),
  })
  const restore = useMutation({
    mutationFn: (backup: SyceeBackupDocument) => syceeDataBackupApi.restore(backup),
    onSuccess: async () => {
      setConfirmRestore(false)
      setSelected(null)
      await queryClient.invalidateQueries({ queryKey: ['sycee'] })
      toast('Sycee 数据已恢复，原数据已保存为安全副本', 'success')
    },
    onError: error => toast(error instanceof Error ? error.message : '数据恢复失败', 'error'),
  })

  const handleFile = async (file: File | undefined) => {
    if (!file) return
    if (file.size > MAX_BACKUP_FILE_BYTES) {
      toast('备份文件不能超过 20 MB', 'error')
      return
    }
    try {
      const document = parseBackupFile(await file.text())
      setSelected({ name: file.name, size: file.size, document })
    } catch (error) {
      setSelected(null)
      toast(error instanceof Error ? error.message : '备份文件读取失败', 'error')
    }
  }

  const currentSummary = current.data ? backupSummary(current.data) : null
  const selectedSummary = selected ? backupSummary(selected.document) : null

  return (
    <>
      <PageHeader
        title="数据备份"
        icon={navMeta?.icon}
        group={navMeta?.group}
        right={(
          <button
            type="button"
            onClick={() => exportData.mutate()}
            disabled={exportData.isPending}
            className="inline-flex min-h-11 items-center justify-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 lg:min-h-9"
          >
            {exportData.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
            {exportData.isPending ? '导出中' : '导出快照'}
          </button>
        )}
      />

      <div className="space-y-4 p-3 lg:p-5">
        <section aria-labelledby="backup-current-title" className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex min-h-12 items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <DatabaseBackup className="h-4 w-4 shrink-0 text-accent" />
              <h2 id="backup-current-title" className="text-sm font-semibold text-foreground">当前 Sycee 数据</h2>
            </div>
            {current.isFetching && <Loader2 className="h-4 w-4 animate-spin text-muted" aria-label="读取中" />}
          </div>

          {current.isError ? (
            <div className="flex min-h-36 flex-col items-center justify-center gap-3 px-6 text-center">
              <AlertTriangle className="h-6 w-6 text-danger" />
              <p className="text-sm text-danger">数据快照校验失败</p>
              <button type="button" onClick={() => current.refetch()} className="inline-flex min-h-10 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated">
                <RefreshCw className="h-3.5 w-3.5" />重新读取
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-2 divide-x divide-y divide-border sm:grid-cols-5 sm:divide-y-0">
              {[
                ['交易流水', currentSummary?.trades ?? '--'],
                ['交易复盘', currentSummary?.reviews ?? '--'],
                ['研究记录', currentSummary?.researchEntries ?? '--'],
                ['策略跟踪', currentSummary?.strategyTracks ?? '--'],
                ['提醒配置', currentSummary ? (currentSummary.sellAlertSaved ? '已保存' : '无') : '--'],
              ].map(([label, value]) => (
                <div key={label} className="px-4 py-4">
                  <div className="text-[11px] text-muted">{label}</div>
                  <div className="mt-1 font-mono text-lg font-semibold text-foreground">{value}</div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section aria-labelledby="backup-restore-title" className="overflow-hidden rounded-card border border-border bg-surface">
          <div className="flex min-h-12 items-center gap-2.5 border-b border-border px-4 py-3">
            <ArchiveRestore className="h-4 w-4 text-g-research" />
            <h2 id="backup-restore-title" className="text-sm font-semibold text-foreground">从快照恢复</h2>
          </div>
          <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
            <div className="min-w-0">
              <input
                ref={fileInputRef}
                type="file"
                accept="application/json,.json"
                className="hidden"
                onChange={event => {
                  void handleFile(event.target.files?.[0])
                  event.currentTarget.value = ''
                }}
              />
              {selected && selectedSummary ? (
                <div className="flex min-w-0 items-start gap-3">
                  <FileJson className="mt-0.5 h-5 w-5 shrink-0 text-g-research" />
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-foreground" title={selected.name}>{selected.name}</div>
                    <div className="mt-1 text-xs text-muted">
                      {formatTime(selected.document.exported_at)} · {formatSize(selected.size)}
                    </div>
                    <div className="mt-1 font-mono text-[11px] text-secondary">
                      {selectedSummary.trades} 笔交易 · {selectedSummary.reviews} 条复盘 · {selectedSummary.researchEntries} 条研究 · {selectedSummary.strategyTracks} 项策略跟踪
                    </div>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-3 text-sm text-muted">
                  <FileJson className="h-5 w-5 shrink-0" />未选择快照文件
                </div>
              )}
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <button type="button" onClick={() => fileInputRef.current?.click()} className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-4 text-sm text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent lg:min-h-9">
                <Upload className="h-4 w-4" />选择文件
              </button>
              <button type="button" onClick={() => setConfirmRestore(true)} disabled={!selected || restore.isPending} className="inline-flex min-h-11 items-center gap-2 rounded-btn bg-danger px-4 text-sm font-medium text-white hover:bg-danger/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-40 lg:min-h-9">
                <ArchiveRestore className="h-4 w-4" />恢复数据
              </button>
            </div>
          </div>
          <div className="border-t border-border bg-danger/5 px-4 py-2.5 text-xs leading-5 text-danger">
            恢复会替换当前 Sycee 数据；现有文件会先保存为服务器安全副本。
          </div>
        </section>
      </div>

      <ConfirmDialog
        open={confirmRestore}
        title="恢复这份 Sycee 数据？"
        message="当前持仓、卖出提醒、交易复盘、研究账本和策略跟踪将被快照内容替换。"
        confirmText="恢复数据"
        danger
        pending={restore.isPending}
        onCancel={() => setConfirmRestore(false)}
        onConfirm={() => { if (selected) restore.mutate(selected.document) }}
      />
    </>
  )
}
