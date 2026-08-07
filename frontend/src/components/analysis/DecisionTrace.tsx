import { LockKeyhole } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { AnalysisTraceNode } from '@/lib/api'

const STATUS_CLASS: Record<string, string> = {
  pass: 'border-success/30 bg-success/5 text-success',
  fail: 'border-danger/30 bg-danger/5 text-danger',
  unknown: 'border-warning/30 bg-warning/5 text-warning',
  skipped: 'border-border bg-elevated/40 text-muted',
}

/**
 * 只读决策链。程序节点由 locked 标识；模型节点只能解释，不能覆盖程序事实。
 * 采用有序列表而非“买/卖决策树”，避免把计划检查误读为交易信号。
 */
export function DecisionTrace({ nodes }: { nodes: AnalysisTraceNode[] }) {
  if (!nodes.length) return null
  const labels = new Map(nodes.map(node => [node.id, node.label]))

  return (
    <div className="space-y-2" aria-label="计划检查决策链">
      {nodes.map((node, index) => (
        <div key={node.id} className="relative pl-5">
          {index < nodes.length - 1 && (
            <span className="absolute left-[7px] top-5 h-[calc(100%+0.5rem)] w-px bg-border" aria-hidden />
          )}
          <span
            className={cn(
              'absolute left-0 top-2 h-3.5 w-3.5 rounded-full border-2 border-surface',
              node.status === 'pass' ? 'bg-success' : node.status === 'fail' ? 'bg-danger' : 'bg-warning',
            )}
            aria-hidden
          />
          <div className={cn('rounded-lg border px-3 py-2', STATUS_CLASS[node.status] ?? STATUS_CLASS.unknown)}>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] font-medium text-foreground">{node.label}</span>
              {node.locked && (
                <span className="inline-flex items-center gap-1 rounded bg-base/70 px-1.5 py-0.5 text-[9px] text-muted" title="程序事实，模型不可修改">
                  <LockKeyhole className="h-2.5 w-2.5" />程序锁定
                </span>
              )}
              <span className="ml-auto font-mono text-[9px] uppercase text-muted">{node.status}</span>
            </div>
            {node.reason && <p className="mt-1 text-[10px] leading-relaxed text-secondary">{node.reason}</p>}
            {node.depends_on.length > 0 && (
              <p className="mt-1 text-[9px] text-muted">
                依据：{node.depends_on.map(id => labels.get(id) ?? id).join(' · ')}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
