import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { SIGNAL_OPTIONS, cnSignal } from '@/lib/signals'

interface Props {
  /** 当前选中的信号 ID 列表 */
  signals: string[]
  /** 选中变化回调 */
  onChange: (next: string[]) => void
  /** 买点 / 卖点 — 决定自定义信号的过滤与配色主题 */
  kind: 'entry' | 'exit'
  /** 渲染尺寸: dialog = 选股弹窗紧凑样式; panel = 回测页设置抽屉样式 */
  variant?: 'dialog' | 'panel'
  builtinSignals?: { key: string; label: string }[]
  disabledSignals?: string[]
  disabledSignalHint?: string
}

/**
 * 买卖触发器信号选择 — 选股页弹窗 / 回测页共用。
 *
 * - 内置信号 (signal_*): 全部展示
 * - 自定义信号 (csg_*): 按 kind 过滤 (entry / exit / both)
 * - entry 蓝色主题, exit 橙色主题
 */
export function SignalPicker({ signals, onChange, kind, variant = 'panel', builtinSignals, disabledSignals = [], disabledSignalHint }: Props) {
  const customSignalsQuery = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })

  const customOptions = useMemo(() => {
    const list = (customSignalsQuery.data?.signals ?? [])
      .filter(s => s.enabled && (s.kind === kind || s.kind === 'both'))
    const names: Record<string, string> = {}
    for (const cs of list) names[`csg_${cs.id}`] = cs.name
    return { list, names }
  }, [customSignalsQuery.data, kind])

  const toggle = (sig: string) => {
    const next = signals.includes(sig) ? signals.filter(x => x !== sig) : [...signals, sig]
    onChange(next)
  }

  // 配色: entry 蓝色 (accent), exit 橙色 (warning/amber)
  const isEntry = kind === 'entry'
  const active = isEntry
    ? 'border-accent/50 bg-accent/10 text-accent'
    : 'border-warning/50 bg-warning/10 text-warning'
  const idle = variant === 'dialog'
    ? 'border-border bg-base text-muted hover:border-accent/40'
    : 'border-border bg-base text-muted hover:border-accent/40'
  const customActive = isEntry
    ? 'border-accent/50 bg-accent/10 text-accent'
    : 'border-warning/50 bg-warning/10 text-warning'
  const customIdle = 'border-amber-400/30 bg-amber-400/5 text-secondary hover:border-amber-400/50 hover:text-amber-400'

  const btnCls = variant === 'dialog'
    ? 'rounded px-1.5 py-0.5 text-[10px] font-medium border transition-colors cursor-pointer'
    : 'rounded-btn border px-2.5 py-1.5 text-[11px] transition-colors cursor-pointer'
  const builtinOptions = builtinSignals ?? SIGNAL_OPTIONS.map(key => ({ key, label: cnSignal(key) }))

  return (
    <div className="flex flex-wrap gap-1.5">
      {builtinOptions.map(option => {
        const disabled = disabledSignals.includes(option.key) && !signals.includes(option.key)
        return (
          <button
            key={option.key}
            type="button"
            disabled={disabled}
            title={disabled ? disabledSignalHint : undefined}
            onClick={() => toggle(option.key)}
            className={`${btnCls} ${signals.includes(option.key) ? active : idle} disabled:cursor-not-allowed disabled:opacity-40`}
          >
            {option.label}
          </button>
        )
      })}
      {customOptions.list.map(cs => {
        const id = `csg_${cs.id}`
        return (
          <button
            key={id}
            type="button"
            onClick={() => toggle(id)}
            title="自定义信号"
            className={`${btnCls} ${signals.includes(id) ? customActive : customIdle}`}
          >
            {customOptions.names[id]}
          </button>
        )
      })}
    </div>
  )
}
