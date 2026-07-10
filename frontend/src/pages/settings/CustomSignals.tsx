import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Zap, Settings2, Lock } from 'lucide-react'
import { api, type CustomSignal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { BUILTIN_SIGNAL_DEFINITIONS, type SignalKind } from '@/lib/signals'
import { CustomSignalDialog } from '@/components/signals/CustomSignalDialog'
import { Skeleton } from '@/components/data/Skeleton'

type SignalSection = 'builtin' | 'custom'

const KIND_LABEL: Record<SignalKind, string> = { entry: '买入', exit: '卖出', both: '买卖通用' }
const KIND_CLASS: Record<SignalKind, string> = {
  entry: 'bg-accent/10 text-accent',
  exit: 'bg-warning/10 text-warning',
  both: 'bg-muted/10 text-muted',
}

export function SettingsCustomSignalsPanel() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions })

  const [activeSection, setActiveSection] = useState<SignalSection>('builtin')
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<CustomSignal | null>(null)
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null)
  const resetDeleteTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fields = options.data?.fields ?? []
  const signals = list.data?.signals ?? []
  const enabledCustomSignals = signals.filter(sig => sig.enabled).length
  const tabs = [
    { key: 'builtin' as const, label: '内置信号', count: BUILTIN_SIGNAL_DEFINITIONS.length, hint: '系统提供，只读' },
    { key: 'custom' as const, label: '自定义信号', count: signals.length, hint: `${enabledCustomSignals} 个已启用` },
  ]

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

  return (
    <div className="max-w-6xl space-y-6">
      <section className="rounded-2xl border border-border bg-surface p-6 bg-[radial-gradient(circle_at_top_right,rgba(234,179,8,0.12),transparent_38%)]">
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="text-[11px] uppercase tracking-[0.2em] text-amber-400/80">信号库</div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">统一查看策略、回测与监控可用信号</h2>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary">
              内置信号由系统预计算，作为只读信号库展示；自定义信号可用「字段 + 运算符 + 值」组合条件创建，保存后可在策略、回测与监控中选择使用。
            </p>
          </div>
          <button
            onClick={openNew}
            className="inline-flex items-center justify-center gap-1.5 rounded-btn bg-amber-500/90 px-3 py-1.5 text-xs font-medium text-base hover:bg-amber-500 transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            新建自定义信号
          </button>
        </div>

        <div className="mt-5 grid grid-cols-1 gap-3 md:grid-cols-3">
          <StatCard label="内置信号" value={BUILTIN_SIGNAL_DEFINITIONS.length} hint="系统提供，只读" />
          <StatCard label="自定义信号" value={signals.length} hint="用户创建，可编辑" />
          <StatCard label="已启用自定义" value={enabledCustomSignals} hint="会注入 csg_* 列" />
        </div>

        <div className="mt-5 rounded-card border border-border bg-base/60 p-1.5">
          <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
            {tabs.map(tab => {
              const active = activeSection === tab.key
              return (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => setActiveSection(tab.key)}
                  className={`rounded-btn px-4 py-3 text-left transition-colors ${active ? 'bg-amber-500/15 text-amber-300 shadow-sm' : 'text-secondary hover:bg-elevated hover:text-foreground'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-medium">{tab.label}</span>
                    <span className={`rounded px-2 py-0.5 text-[11px] ${active ? 'bg-amber-400/15 text-amber-300' : 'bg-elevated text-muted'}`}>{tab.count}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-muted">{tab.hint}</div>
                </button>
              )
            })}
          </div>
        </div>
      </section>

      {activeSection === 'builtin' && (
        <section className="rounded-card border border-border bg-surface p-5 space-y-4">
          <div className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <Lock className="h-3.5 w-3.5 text-muted" />
                <h3 className="text-sm font-medium text-foreground">内置信号</h3>
                <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">只读</span>
              </div>
              <p className="mt-1 text-xs text-muted">这些信号由系统在 enriched 数据中预计算，策略选择器会直接展示。</p>
            </div>
            <div className="text-[11px] text-muted">ID 前缀：<span className="font-mono text-foreground/70">signal_</span></div>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {BUILTIN_SIGNAL_DEFINITIONS.map(sig => (
              <div key={sig.id} className="rounded-card border border-border bg-base p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h4 className="text-sm font-medium text-foreground truncate">{sig.name}</h4>
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${KIND_CLASS[sig.kind]}`}>
                        {KIND_LABEL[sig.kind]}
                      </span>
                    </div>
                    <p className="mt-1 text-[11px] text-muted font-mono truncate">{sig.id}</p>
                  </div>
                  <span className="shrink-0 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-muted">{sig.category}</span>
                </div>
                <p className="mt-3 text-xs leading-5 text-secondary">{sig.description}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {activeSection === 'custom' && (
        <section className="rounded-card border border-border bg-surface p-5 space-y-4">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <Zap className="h-3.5 w-3.5 text-amber-400" />
                <h3 className="text-sm font-medium text-foreground">自定义信号</h3>
                <span className="rounded bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-400">可配置</span>
              </div>
              <p className="mt-1 text-xs text-muted">这些信号由你定义，可启用/停用，并在策略、回测与监控中作为 csg_* 信号使用。</p>
            </div>
            <button
              onClick={openNew}
              className="inline-flex items-center justify-center gap-1.5 rounded-btn border border-amber-400/30 bg-amber-400/5 px-3 py-1.5 text-xs font-medium text-amber-400 hover:bg-amber-400/10 transition-colors"
            >
              <Plus className="h-3.5 w-3.5" />
              新建自定义信号
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {signals.map(sig => (
              <div key={sig.id} className="rounded-card border border-border bg-base p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-medium text-foreground truncate">{sig.name}</h3>
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${KIND_CLASS[sig.kind]}`}>
                        {KIND_LABEL[sig.kind]}
                      </span>
                      {!sig.enabled && <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">已停用</span>}
                    </div>
                    <p className="mt-1 text-[11px] text-muted font-mono truncate">csg_{sig.id}</p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button onClick={() => toggleEnabled(sig)} title={sig.enabled ? '停用' : '启用'} className={`p-1 rounded cursor-pointer ${sig.enabled ? 'text-emerald-400 hover:bg-emerald-400/10' : 'text-muted hover:bg-elevated'}`}>
                      <Zap className="h-3.5 w-3.5" />
                    </button>
                    <button onClick={() => openEdit(sig)} className="p-1 rounded text-muted hover:text-accent hover:bg-accent/10 cursor-pointer" title="编辑">
                      <Settings2 className="h-3.5 w-3.5" />
                    </button>
                    {confirmingDeleteId === sig.id ? (
                      <button
                        onClick={() => handleDeleteClick(sig)}
                        disabled={del.isPending}
                        title="再次点击确认删除"
                        className="inline-flex items-center gap-1 rounded-md bg-danger/15 px-1.5 py-0.5 text-[10px] font-medium text-danger border border-danger/30 animate-pulse cursor-pointer disabled:opacity-50"
                      >
                        <Trash2 className="h-2.5 w-2.5" />确认
                      </button>
                    ) : (
                      <button
                        onClick={() => handleDeleteClick(sig)}
                        disabled={del.isPending}
                        className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer disabled:opacity-50"
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
                      <span className="text-muted/50 w-6 text-right">{i === 0 ? '当' : '且'}</span>
                      <span className="font-mono text-foreground/80">{fieldLabel(c.left, fields)}</span>
                      <span className="font-mono text-muted">{c.op}</span>
                      <span className="font-mono text-foreground/80">{rightDisplay(c.right, fields)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {list.isLoading &&
              Array.from({ length: 2 }).map((_, i) => (
                <div key={`sk-${i}`} className="rounded-card border border-border bg-base p-4 space-y-3">
                  <Skeleton w="w-1/2" h="h-4" />
                  <Skeleton w="w-1/3" h="h-3" />
                  <Skeleton h="h-4" />
                </div>
              ))}
            {!list.isLoading && signals.length === 0 && (
              <div className="rounded-card border border-border bg-base px-5 py-10 text-center text-sm text-muted md:col-span-2">
                暂无自定义信号，点击右上角「新建自定义信号」。
              </div>
            )}
          </div>
        </section>
      )}

      <CustomSignalDialog open={showForm} signal={editing} onClose={closeForm} />
    </div>
  )
}

function StatCard({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="rounded-card border border-border/80 bg-base/70 px-4 py-3">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-foreground">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted">{hint}</div>
    </div>
  )
}

function fieldLabel(key: string, fields: { key: string; label: string }[]): string {
  return fields.find(f => f.key === key)?.label ?? key
}

function rightDisplay(right: string, fields: { key: string; label: string }[]): string {
  if (right.startsWith('field:')) return fieldLabel(right.slice(6), fields)
  return right
}
