import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, ExternalLink, Link2, Loader2, RefreshCw, ShieldOff, X } from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'
import { researchLedgerApi, type ResearchEntry } from './api'
import { isResearchShareStale, researchShareUrl } from './sharing'

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

export function ResearchShareDialog({ entry, onClose }: {
  entry: ResearchEntry
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [confirmRevoke, setConfirmRevoke] = useState(false)
  const queryKey = ['sycee', 'research-share', entry.id] as const
  const shareQuery = useQuery({
    queryKey,
    queryFn: () => researchLedgerApi.getShare(entry.id),
  })
  const share = shareQuery.data?.share ?? null
  const stale = share ? isResearchShareStale(share, entry) : false

  const create = useMutation({
    mutationFn: () => researchLedgerApi.createShare(entry.id),
    onSuccess: data => {
      queryClient.setQueryData(queryKey, data)
      toast('只读分享已创建', 'success')
    },
    onError: cause => toast(cause instanceof Error ? cause.message : '分享创建失败', 'error'),
  })
  const refresh = useMutation({
    mutationFn: () => researchLedgerApi.refreshShare(entry.id),
    onSuccess: data => {
      queryClient.setQueryData(queryKey, data)
      toast('公开快照已更新', 'success')
    },
    onError: cause => toast(cause instanceof Error ? cause.message : '公开快照更新失败', 'error'),
  })
  const revoke = useMutation({
    mutationFn: () => researchLedgerApi.revokeShare(entry.id),
    onSuccess: () => {
      queryClient.setQueryData(queryKey, { share: null })
      setConfirmRevoke(false)
      toast('只读分享已撤销', 'success')
    },
    onError: cause => toast(cause instanceof Error ? cause.message : '分享撤销失败', 'error'),
  })

  const copy = async () => {
    if (!share) return
    try {
      await navigator.clipboard.writeText(researchShareUrl(window.location.origin, share.token))
      toast('分享链接已复制', 'success')
    } catch {
      toast('无法访问剪贴板', 'error')
    }
  }

  const pending = create.isPending || refresh.isPending || revoke.isPending

  return (
    <>
      <Modal onClose={() => { if (!pending) onClose() }} labelledBy="research-share-title" panelClassName="w-[94vw] max-w-xl overflow-hidden rounded-dialog border border-border bg-surface shadow-2xl">
        <div className="flex min-h-14 items-center justify-between border-b border-border px-4">
          <div className="min-w-0">
            <h2 id="research-share-title" className="text-sm font-semibold text-foreground">只读分享</h2>
            <p className="mt-0.5 truncate text-[10px] text-muted">{entry.title}</p>
          </div>
          <button type="button" title="关闭" aria-label="关闭" disabled={pending} onClick={onClose} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-btn text-muted hover:bg-elevated hover:text-foreground disabled:opacity-50"><X className="h-4 w-4" /></button>
        </div>

        <div className="p-4 sm:p-5">
          {shareQuery.isLoading ? (
            <div className="flex min-h-40 items-center justify-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" />读取分享状态</div>
          ) : shareQuery.isError ? (
            <div className="flex min-h-40 flex-col items-center justify-center gap-3 text-center">
              <p className="text-sm text-danger">分享状态读取失败</p>
              <button type="button" onClick={() => { void shareQuery.refetch() }} className="inline-flex min-h-10 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated"><RefreshCw className="h-3.5 w-3.5" />重试</button>
            </div>
          ) : share ? (
            <div className="space-y-4">
              <div className="rounded-card border border-bull/25 bg-bull/5 p-3.5">
                <div className="flex items-center gap-2 text-xs font-medium text-bull"><Link2 className="h-4 w-4" />公开快照已启用</div>
                <div className="mt-2 break-all rounded-input border border-border bg-base px-3 py-2 font-mono text-[11px] leading-5 text-secondary">{researchShareUrl(window.location.origin, share.token)}</div>
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted">
                  <span>创建 {formatTime(share.created_at)}</span>
                  <span>快照 {formatTime(share.refreshed_at)}</span>
                </div>
              </div>

              {stale && (
                <div className="rounded-card border border-warning/30 bg-warning/10 px-3 py-2.5 text-xs leading-5 text-warning">私有记录已有更新，当前公开链接仍显示上次快照。</div>
              )}

              <div className="flex flex-wrap justify-end gap-2">
                <button type="button" onClick={() => { void copy() }} className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:min-h-9"><Copy className="h-3.5 w-3.5" />复制链接</button>
                <a href={researchShareUrl(window.location.origin, share.token)} target="_blank" rel="noreferrer" className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:min-h-9"><ExternalLink className="h-3.5 w-3.5" />打开</a>
                <button type="button" disabled={pending} onClick={() => refresh.mutate()} className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 sm:min-h-9">{refresh.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}更新快照</button>
                <button type="button" disabled={pending} onClick={() => setConfirmRevoke(true)} className="inline-flex min-h-11 items-center gap-2 rounded-btn border border-danger/25 px-3 text-xs text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-50 sm:min-h-9"><ShieldOff className="h-3.5 w-3.5" />撤销</button>
              </div>
            </div>
          ) : (
            <div className="flex min-h-44 flex-col items-center justify-center rounded-card border border-dashed border-border px-5 text-center">
              <Link2 className="h-7 w-7 text-muted" />
              <h3 className="mt-3 text-sm font-medium text-foreground">尚未创建公开快照</h3>
              <p className="mt-1 text-xs leading-5 text-muted">链接无需登录即可查看，公开内容不包含账户身份和捕获原始字段。</p>
              <button type="button" disabled={create.isPending} onClick={() => create.mutate()} className="mt-4 inline-flex min-h-11 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50">{create.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}创建分享</button>
            </div>
          )}
        </div>
      </Modal>

      <ConfirmDialog
        open={confirmRevoke}
        title="撤销只读分享？"
        message="当前公开链接将立即失效，私有研究记录不会删除。"
        confirmText="撤销分享"
        danger
        pending={revoke.isPending}
        onCancel={() => setConfirmRevoke(false)}
        onConfirm={() => revoke.mutate()}
      />
    </>
  )
}
