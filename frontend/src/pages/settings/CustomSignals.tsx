import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Zap, Settings2, Lock } from 'lucide-react'
import { api, type CustomSignal } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { BUILTIN_SIGNAL_DEFINITIONS, type SignalKind } from '@/lib/signals'
import { CustomSignalDialog } from '@/components/signals/CustomSignalDialog'
import { Skeleton } from '@/components/data/Skeleton'
import { cn } from '@/lib/cn'
import {
  SettingsPanel,
  SettingsSection,
  settingsIconButtonClass,
  settingsPrimaryButtonClass,
  settingsSecondaryButtonClass,
} from './SettingsPrimitives'

type SignalSection = 'builtin' | 'custom'

const KIND_LABEL: Record<SignalKind, string> = { entry: '入场', exit: '出场', both: '出入通用' }
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
    <>
      <SettingsPanel
      icon={Zap}
      title="信号库"
      description="统一查看策略、回测与监控可用信号；内置信号只读，自定义信号保存后可在各功能中选择使用。"
      width="wide"
      action={
        <button type="button" onClick={openNew} className={settingsPrimaryButtonClass}>
          <Plus className="h-3.5 w-3.5" />
          新建自定义信号
        </button>
      }
    >
      <SettingsSection
        title="信号概览"
        description="查看信号数量，并在内置信号和自定义信号之间切换。"
        badge={`${BUILTIN_SIGNAL_DEFINITIONS.length + signals.length} 项`}
        contentClassName="space-y-4"
      >
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
          <StatCard label="内置信号" value={BUILTIN_SIGNAL_DEFINITIONS.length} hint="系统提供，只读" />
          <StatCard label="自定义信号" value={signals.length} hint="用户创建，可编辑" />
          <StatCard label="已启用自定义" value={enabledCustomSignals} hint="会注入 csg_* 列" />
        </div>

        <div className="rounded-btn bg-base/60 p-1">
          <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
            {tabs.map(tab => {
              const active = activeSection === tab.key
              return (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => setActiveSection(tab.key)}
                  className={cn(
                    'rounded-btn border px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50',
                    active
                      ? 'border-accent/25 bg-accent/10 text-foreground'
                      : 'border-transparent text-secondary hover:bg-elevated hover:text-foreground',
                  )}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-medium">{tab.label}</span>
                    <span className={cn(
                      'rounded px-2 py-0.5 text-[11px]',
                      active ? 'bg-accent/15 text-accent' : 'bg-elevated text-muted',
                    )}>
                      {tab.count}
                    </span>
                  </div>
                  <div className="mt-1 text-[11px] text-muted">{tab.hint}</div>
                </button>
              )
            })}
          </div>
        </div>
      </SettingsSection>

      {activeSection === 'builtin' && (
        <SettingsSection
          icon={Lock}
          title="内置信号"
          description="这些信号由系统在 enriched 数据中预计算，策略选择器会直接展示。"
          badge="只读"
          unframed
          action={
            <div className="hidden text-[11px] text-muted sm:block">
              ID 前缀：<span className="font-mono text-foreground/70">signal_</span>
            </div>
          }
        >
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
        </SettingsSection>
      )}

      {activeSection === 'custom' && (
        <SettingsSection
          icon={Zap}
          title="自定义信号"
          description="由你定义，可启用或停用，并在策略、回测与监控中作为 csg_* 信号使用。"
          badge="可配置"
          unframed
          action={
            <button
              type="button"
              onClick={openNew}
              className={cn(settingsSecondaryButtonClass, 'h-11 px-2 sm:h-8 sm:px-3')}
            >
              <Plus className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">新建自定义信号</span>
              <span className="sm:hidden">新建</span>
            </button>
          }
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {signals.map(sig => (
              <div key={sig.id} className="rounded-card border border-border bg-base p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h4 className="text-sm font-medium text-foreground truncate">{sig.name}</h4>
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${KIND_CLASS[sig.kind]}`}>
                        {KIND_LABEL[sig.kind]}
                      </span>
                      {!sig.enabled && <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">已停用</span>}
                    </div>
                    <p className="mt-1 text-[11px] text-muted font-mono truncate">csg_{sig.id}</p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      type="button"
                      onClick={() => toggleEnabled(sig)}
                      title={sig.enabled ? '停用' : '启用'}
                      className={cn(settingsIconButtonClass, sig.enabled && 'text-emerald-400 hover:bg-emerald-400/10')}
                    >
                      <Zap className="h-3.5 w-3.5" />
                    </button>
                    <button type="button" onClick={() => openEdit(sig)} className={settingsIconButtonClass} title="编辑">
                      <Settings2 className="h-3.5 w-3.5" />
                    </button>
                    {confirmingDeleteId === sig.id ? (
                      <button
                        type="button"
                        onClick={() => handleDeleteClick(sig)}
                        disabled={del.isPending}
                        title="再次点击确认删除"
                        className="inline-flex h-11 items-center gap-1 rounded-btn border border-danger/30 bg-danger/15 px-2 text-[10px] font-medium text-danger animate-pulse disabled:opacity-50 sm:h-8"
                      >
                        <Trash2 className="h-2.5 w-2.5" />确认
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => handleDeleteClick(sig)}
                        disabled={del.isPending}
                        className={cn(settingsIconButtonClass, 'hover:bg-danger/10 hover:text-danger')}
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
        </SettingsSection>
      )}
      </SettingsPanel>
      <CustomSignalDialog open={showForm} signal={editing} onClose={closeForm} />
    </>
  )
}

function StatCard({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="rounded-btn bg-base/70 px-3 py-2.5">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums text-foreground">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted">{hint}</div>
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
