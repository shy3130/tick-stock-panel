import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PenLine } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

/** 展示与过滤可选项 (全部可选, 有默认值) */
interface SignalPickerOptions {
  /** 渲染尺寸: dialog = 策略弹窗紧凑样式; panel = 回测页设置抽屉样式 */
  variant?: 'dialog' | 'panel'
  /** 额外信号选项 (监控页传入盘中信号); 策略/回测不传 — 全部信号来自定义 */
  builtinSignals?: { key: string; label: string }[]
  disabledSignals?: string[]
  disabledSignalHint?: string
  /**
   * 是否按 kind 过滤自定义信号。默认 true (策略/回测: 入场区只显示 entry)。
   * 监控规则页设 false: 报警语义是"命中即报", 不分入场出场, 信号全部显示。
   */
  filterCustomByKind?: boolean
}

interface Props {
  /** 当前选中的信号列名列表 (csg_* / 迁移定义的 signal_*) */
  signals: string[]
  /** 选中变化回调 */
  onChange: (next: string[]) => void
  /** 买点 / 卖点 — 决定信号的过滤与配色主题 */
  kind: 'entry' | 'exit'
  options?: SignalPickerOptions
}

/**
 * 买卖触发器信号选择 — 策略页弹窗 / 回测页共用。
 *
 * - 全部信号来自用户定义 (/api/custom-signals), 按解析列名 (csg_* 或迁移
 *   定义的 signal_*) 作为选中值; 按 kind 过滤 (entry / exit / both),
 *   除非 filterCustomByKind=false
 * - builtinSignals 仅在监控页传入 (盘中信号)
 * - entry 蓝色主题, exit 橙色主题; 右上角 PenLine 角标标识可编辑的定义信号
 */
export function SignalPicker({ signals, onChange, kind, options }: Props) {
  const { variant = 'panel', builtinSignals = [], disabledSignals = [], disabledSignalHint, filterCustomByKind = true } = options ?? {}
  const customSignalsQuery = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })

  const customOptions = useMemo(() => {
    const list = (customSignalsQuery.data?.signals ?? [])
      .filter(s => s.enabled && s.timeframe !== 'intraday')
      .filter(s => !filterCustomByKind || s.kind === kind || s.kind === 'both')
    // 列名 = 显式 column (迁移定义沿用 signal_*) 或默认 csg_{id}
    const names: Record<string, string> = {}
    for (const cs of list) names[cs.column ?? `csg_${cs.id}`] = cs.name
    return { list, names }
  }, [customSignalsQuery.data, kind, filterCustomByKind])

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

  const btnCls = variant === 'dialog'
    ? 'rounded px-1.5 py-0.5 text-[10px] font-medium border transition-colors cursor-pointer'
    : 'rounded-btn border px-2.5 py-1.5 text-[11px] transition-colors cursor-pointer'

  return (
    <div className="flex flex-wrap gap-1.5">
      {builtinSignals.map(option => {
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
        const id = cs.column ?? `csg_${cs.id}`
        return (
          <button
            key={id}
            type="button"
            onClick={() => toggle(id)}
            title="信号库定义, 可在信号库编辑"
            className={`${btnCls} relative ${signals.includes(id) ? active : idle}`}
          >
            {customOptions.names[id]}
            <span className="pointer-events-none absolute -top-1 -right-1 rounded-full border border-border bg-base p-px">
              <PenLine className="h-2 w-2 text-muted" />
            </span>
          </button>
        )
      })}
    </div>
  )
}
