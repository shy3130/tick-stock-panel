import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BookOpen,
  CalendarDays,
  CheckCircle2,
  CircleDashed,
  FileQuestion,
  LockKeyhole,
  RadioTower,
  Target,
  XCircle,
} from 'lucide-react'
import { useParams } from 'react-router-dom'

import { Logo } from '@/components/Logo'
import { cn } from '@/lib/cn'
import { BRAND_NAME } from '@/lib/brand'
import { researchLedgerApi, type ResearchStatus } from './api'

const STATUS_META: Record<ResearchStatus, { label: string; className: string; icon: typeof CircleDashed }> = {
  draft: { label: '草稿', className: 'border-border bg-elevated text-secondary', icon: CircleDashed },
  tracking: { label: '跟踪中', className: 'border-accent/30 bg-accent/10 text-accent', icon: Target },
  validated: { label: '已验证', className: 'border-bull/30 bg-bull/10 text-bull', icon: CheckCircle2 },
  invalidated: { label: '已失效', className: 'border-danger/30 bg-danger/10 text-danger', icon: XCircle },
  archived: { label: '已归档', className: 'border-border bg-base text-muted', icon: BookOpen },
}

const SUBJECT_LABEL = {
  stock: '个股',
  strategy: '策略',
  sector: '板块',
  market: '市场',
} as const

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function EvidenceList({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <p className="text-sm leading-6 text-muted">{empty}</p>
  return (
    <ul className="space-y-2">
      {items.map((item, index) => (
        <li key={`${index}-${item}`} className="flex gap-2 text-sm leading-6 text-secondary">
          <span className="mt-[9px] h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-60" aria-hidden="true" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  )
}

export function ResearchSharePage() {
  const token = useParams<{ token: string }>().token ?? ''
  const share = useQuery({
    queryKey: ['sycee', 'public-research-share', token],
    queryFn: () => researchLedgerApi.publicShare(token),
    enabled: token.length > 0,
    retry: false,
  })

  useEffect(() => {
    const previous = document.title
    if (share.data?.entry.title) document.title = `${share.data.entry.title} · ${BRAND_NAME}`
    return () => { document.title = previous }
  }, [share.data?.entry.title])

  if (share.isLoading) {
    return <div className="grid min-h-screen place-items-center bg-base text-xs text-muted">读取研究快照</div>
  }

  if (share.isError || !share.data) {
    return (
      <div className="grid min-h-screen place-items-center bg-base px-5">
        <div className="text-center">
          <FileQuestion className="mx-auto h-9 w-9 text-muted" />
          <h1 className="mt-4 text-lg font-semibold text-foreground">分享不存在或已撤销</h1>
          <p className="mt-2 text-sm text-muted">请向分享者确认当前链接。</p>
        </div>
      </div>
    )
  }

  const { entry } = share.data
  const status = STATUS_META[entry.status]
  const StatusIcon = status.icon

  return (
    <div className="min-h-screen bg-base text-foreground">
      <header className="border-b border-border bg-surface/80">
        <div className="mx-auto flex min-h-14 max-w-4xl items-center justify-between gap-4 px-4 sm:px-6">
          <div className="flex items-center gap-2.5"><Logo size={24} /><span className="text-sm font-semibold">{BRAND_NAME} Research</span></div>
          <div className="inline-flex items-center gap-1.5 text-[10px] text-muted"><LockKeyhole className="h-3.5 w-3.5" />只读快照</div>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-6 sm:px-6 sm:py-10">
        <article className="overflow-hidden rounded-card border border-border bg-surface">
          <header className="border-b border-border px-4 py-5 sm:px-7 sm:py-7">
            <div className="flex flex-wrap items-center gap-2">
              <span className={cn('inline-flex items-center gap-1 rounded-input border px-2 py-1 text-[11px] font-medium', status.className)}><StatusIcon className="h-3 w-3" />{status.label}</span>
              <span className="rounded-input border border-border bg-base px-2 py-1 text-[11px] text-secondary">{SUBJECT_LABEL[entry.subject_type]}</span>
              {entry.subject && <span className="font-mono text-[11px] text-muted">{entry.subject}</span>}
            </div>
            <h1 className="mt-4 text-2xl font-semibold leading-9 text-foreground sm:text-3xl">{entry.title}</h1>
            {entry.tags.length > 0 && <div className="mt-3 flex flex-wrap gap-1.5">{entry.tags.map(tag => <span key={tag} className="rounded-input bg-accent/10 px-2 py-1 text-[10px] text-accent">{tag}</span>)}</div>}
            <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted"><span className="inline-flex items-center gap-1"><CalendarDays className="h-3 w-3" />记录更新 {formatTime(entry.updated_at)}</span><span>快照更新 {formatTime(share.data.refreshed_at)}</span></div>
          </header>

          <div className="px-4 sm:px-7">
            {entry.captures.length > 0 && (
              <section className="border-b border-border py-5 sm:py-6">
                <h2 className="flex items-center gap-2 text-xs font-semibold text-accent"><RadioTower className="h-4 w-4" />来源记录</h2>
                <ol className="mt-4 space-y-3 border-l border-accent/25 pl-4">
                  {entry.captures.map((capture, index) => (
                    <li key={`${capture.captured_at}-${index}`} className="relative">
                      <span className="absolute -left-[19px] top-1.5 h-1.5 w-1.5 rounded-full bg-accent" aria-hidden="true" />
                      <div className="flex flex-wrap items-center gap-2"><span className="text-[10px] font-medium text-accent">{capture.source_label}</span><time className="font-mono text-[10px] text-muted">{formatTime(capture.captured_at)}</time></div>
                      <p className="mt-1 text-sm leading-6 text-secondary">{capture.summary}</p>
                    </li>
                  ))}
                </ol>
              </section>
            )}

            <section className="border-b border-border py-5 sm:py-6">
              <h2 className="text-xs font-semibold text-muted">核心判断</h2>
              <p className={cn('mt-3 whitespace-pre-wrap text-sm leading-7 sm:text-base', entry.thesis ? 'text-foreground' : 'text-muted')}>{entry.thesis || '未记录核心判断。'}</p>
            </section>

            <div className="grid sm:grid-cols-2">
              <section className="border-b border-border py-5 sm:border-r sm:pr-6"><h2 className="text-xs font-semibold text-bull">支持证据</h2><div className="mt-3"><EvidenceList items={entry.evidence} empty="未记录支持证据。" /></div></section>
              <section className="border-b border-border py-5 sm:pl-6"><h2 className="text-xs font-semibold text-danger">反方证据</h2><div className="mt-3"><EvidenceList items={entry.counter_evidence} empty="未记录反方证据。" /></div></section>
            </div>

            <div className="grid sm:grid-cols-2">
              <section className="border-b border-border py-5 sm:border-r sm:pr-6"><h2 className="text-xs font-semibold text-warning">失效条件</h2><p className={cn('mt-3 whitespace-pre-wrap text-sm leading-7', entry.invalidation ? 'text-secondary' : 'text-muted')}>{entry.invalidation || '未定义失效条件。'}</p></section>
              <section className="border-b border-border py-5 sm:pl-6"><h2 className="text-xs font-semibold text-accent">下一步</h2><p className={cn('mt-3 whitespace-pre-wrap text-sm leading-7', entry.plan ? 'text-secondary' : 'text-muted')}>{entry.plan || '未安排下一步。'}</p></section>
            </div>

            <footer className="flex flex-col gap-1 py-4 text-[10px] text-muted sm:flex-row sm:items-center sm:justify-between"><span>创建于 {formatTime(entry.created_at)}</span><span>公开于 {formatTime(share.data.published_at)}</span></footer>
          </div>
        </article>
      </main>
    </div>
  )
}
