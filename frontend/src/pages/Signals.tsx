import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Lock, Plus, Settings2, Trash2, Zap } from 'lucide-react'
import { api, type CustomSignal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { BUILTIN_SIGNAL_DEFINITIONS, type SignalKind } from '@/lib/signals'
import { CustomSignalDialog } from '@/components/signals/CustomSignalDialog'
import { Skeleton } from '@/components/data/Skeleton'
import { PageHeader } from '@/components/PageHeader'
import { AnchorWrap } from '@/lib/useCardFlash'

type SignalSection = 'builtin' | 'custom'

const KIND_LABEL: Record<SignalKind, string> = { entry: '入场', exit: '出场', both: '出入通用' }
const KIND_CLASS: Record<SignalKind, string> = {
  entry: 'bg-accent/10 text-accent',
  exit: 'bg-warning/10 text-warning',
  both: 'bg-muted/10 text-muted',
}

/** 信号库独立页: 内置只读信号 + 自定义条件信号 (csg_*), 策略/回测/监控统一取用。 */
export function Signals() {
  const [searchParams] = useSearchParams()
  const highlight = searchParams.get('highlight') ?? ''

  return (
    <div className="flex flex-col h-full">
      <PageHeader title="信号库" subtitle="内置预计算信号与自定义条件信号, 供策略 / 回测 / 监控统一使用" />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="mx-auto max-w-6xl">
          <SignalsBody highlight={highlight} />
        </div>
      </div>
    </div>
  )
}

function SignalsBody({ highlight }: { highlight: string }) {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList, staleTime: 60_000 })
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions, staleTime: 60_000 })

  const [activeSection, setActiveSection] = useState<SignalSection>('custom')
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<CustomSignal | null>(null)
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null)
  const resetDeleteTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fields = options.data?.fields ?? []
  const signals = list.data?.signals ?? []
  const enabledCustomSignals = signals.filter(sig => sig.enabled).length

  useEffect(() => () => {
    if (resetDeleteTimer.current) clearTimeout(resetDeleteTimer.current)
  }, [])

  const clearDeleteConfirm = () => {
    if (resetDeleteTimer.current) clearTimeout(resetDeleteTimer.current)
    resetDeleteTimer.current = null
    setConfirmingDeleteId(null)
  }

  const openNew = () => {
    setEditing(null)
    clearDeleteConfirm()
    setActiveSection('custom')
    setShowForm(true)
  }
  const openEdit = (sig: CustomSignal) => {
    setEditing(sig)
    clearDeleteConfirm()
    setActiveSection('custom')
    setShowForm(true)
  }
  const closeForm = () => {
    setShowForm(false)
    setEditing(null)
  }

  const del = useMutation({
    mutationFn: api.customSignalDelete,
    onSuccess: () => {
      clearDeleteConfirm()
      qc.invalidateQueries({ queryKey: QK.customSignals })
    },
  })

  const toggleEnabled = (sig: CustomSignal) => {
    api.customSignalSave({ ...sig, enabled: !sig.enabled }).then(() => qc.invalidateQueries({ queryKey: QK.customSignals }))
  }

  const handleDeleteClick = (sig: CustomSignal) => {
    if (confirmingDeleteId === sig.id) {
      clearDeleteConfirm()
      del.mutate(sig.id)
      return
    }
    setConfirmingDeleteId(sig.id)
    if (resetDeleteTimer.current) clearTimeout(resetDeleteTimer.current)
    resetDeleteTimer.current = setTimeout(() => setConfirmingDeleteId(null), 3000)
  }

  const tabs = [
    { key: 'custom' as const, label: '自定义信号', count: signals.length, hint: `${enabledCustomSignals} 个已启用` },
    { key: 'builtin' as const, label: '内置信号', count: BUILTIN_SIGNAL_DEFINITIONS.length, hint: '系统预计算，只读' },
  ]

  return (
    <div className="space-y-4">
      {/* 紧凑工具条: 分段切换 + 新建 */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
          {tabs.map(tab => {
            const active = activeSection === tab.key
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveSection(tab.key)}
                aria-current={active ? 'page' : undefined}
                className={`inline-flex h-7 items-center gap-1.5 rounded-[5px] px-2.5 text-[11px] font-medium transition-colors sm:text-xs ${active
                  ? 'bg-amber-500/90 text-white shadow-sm'
                  : 'text-secondary hover:bg-elevated hover:text-foreground'}`}
              >
                {tab.label}
                <span className={`rounded px-1 py-px text-[10px] font-mono ${active ? 'bg-black/15 text-white' : 'bg-elevated text-muted'}`}>
                  {tab.count}
                </span>
              </button>
            )
          })}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-muted">自定义信号保存为 <span className="font-mono text-secondary">csg_*</span> 列，条件字段可用行情指标与全部注册因子</span>
          <button
            onClick={openNew}
            className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-amber-400/30 bg-amber-400/5 px-3 text-xs font-medium text-amber-400 transition-colors hover:bg-amber-400/10"
          >
            <Plus className="h-3.5 w-3.5" />
            新建信号
          </button>
        </div>
      </div>

      {activeSection === 'custom' && (
        <AnchorWrap highlight={highlight} anchor="signals">
        <section className="rounded-card border border-border bg-surface p-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {signals.map(sig => (
              <div key={sig.id} className="rounded-card border border-border bg-base p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate text-sm font-medium text-foreground">{sig.name}</h3>
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${KIND_CLASS[sig.kind]}`}>
                        {KIND_LABEL[sig.kind]}
                      </span>
                      {!sig.enabled && <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">已停用</span>}
                    </div>
                    <p className="mt-1 truncate font-mono text-[11px] text-muted">csg_{sig.id}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button onClick={() => toggleEnabled(sig)} title={sig.enabled ? '停用' : '启用'} className={`cursor-pointer rounded p-1 ${sig.enabled ? 'text-emerald-400 hover:bg-emerald-400/10' : 'text-muted hover:bg-elevated'}`}>
                      <Zap className="h-3.5 w-3.5" />
                    </button>
                    <button onClick={() => openEdit(sig)} className="cursor-pointer rounded p-1 text-muted hover:bg-accent/10 hover:text-accent" title="编辑">
                      <Settings2 className="h-3.5 w-3.5" />
                    </button>
                    {confirmingDeleteId === sig.id ? (
                      <button
                        onClick={() => handleDeleteClick(sig)}
                        disabled={del.isPending}
                        title="再次点击确认删除"
                        className="inline-flex animate-pulse cursor-pointer items-center gap-1 rounded-md border border-danger/30 bg-danger/15 px-1.5 py-0.5 text-[10px] font-medium text-danger disabled:opacity-50"
                      >
                        <Trash2 className="h-2.5 w-2.5" />确认
                      </button>
                    ) : (
                      <button
                        onClick={() => handleDeleteClick(sig)}
                        disabled={del.isPending}
                        className="cursor-pointer rounded p-1 text-muted hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                        title="删除"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
                <div className="mt-3 space-y-1">
                  {sig.conditions.map((c, i) => (
                    <div key={i} className="flex items-center gap-1.5 text-[11px] text-secondary">
                      <span className="w-6 text-right text-muted/50">{i === 0 ? '当' : '且'}</span>
                      <span className="font-mono text-foreground/80">{fieldWithDays(c.left, c.leftDays, fields)}</span>
                      <span className="font-mono text-muted">{c.op}</span>
                      <span className="font-mono text-foreground/80">
                        {c.right.startsWith('field:')
                          ? fieldWithDays(c.right.slice(6), c.rightDays, fields)
                          : c.right}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {list.isLoading &&
              Array.from({ length: 2 }).map((_, i) => (
                <div key={`sk-${i}`} className="space-y-3 rounded-card border border-border bg-base p-4">
                  <Skeleton w="w-1/2" h="h-4" />
                  <Skeleton w="w-1/3" h="h-3" />
                  <Skeleton h="h-4" />
                </div>
              ))}
            {!list.isLoading && signals.length === 0 && (
              <div className="rounded-card border border-dashed border-border px-5 py-10 text-center text-sm text-muted md:col-span-2">
                暂无自定义信号。可用「字段 + 运算符 + 值」组合条件创建，或从检验页因子行一键生成；也可让 AI 按描述生成。
              </div>
            )}
          </div>
        </section>
        </AnchorWrap>
      )}

      {activeSection === 'builtin' && (
        <section className="rounded-card border border-border bg-surface p-4">
          <div className="mb-3 flex items-center gap-2 text-xs text-muted">
            <Lock className="h-3.5 w-3.5" />
            系统在 enriched 数据中预计算，策略选择器直接展示，只读。
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {BUILTIN_SIGNAL_DEFINITIONS.map(sig => (
              <div key={sig.id} className="rounded-card border border-border bg-base p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h4 className="truncate text-sm font-medium text-foreground">{sig.name}</h4>
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${KIND_CLASS[sig.kind]}`}>
                        {KIND_LABEL[sig.kind]}
                      </span>
                    </div>
                    <p className="mt-1 truncate font-mono text-[11px] text-muted">{sig.id}</p>
                  </div>
                  <span className="shrink-0 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-muted">{sig.category}</span>
                </div>
                <p className="mt-3 text-xs leading-5 text-secondary">{sig.description}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      <CustomSignalDialog open={showForm} signal={editing} onClose={closeForm} />
    </div>
  )
}

function fieldLabel(key: string, fields: { key: string; label: string }[]): string {
  return fields.find(f => f.key === key)?.label ?? key
}

/** 带偏移标注的字段显示: 收盘价(前1日) / MA20(最新省略) */
function fieldWithDays(key: string, days: number | undefined, fields: { key: string; label: string }[]): string {
  const label = fieldLabel(key, fields)
  return days && days > 0 ? `${label}(前${days}日)` : label
}
