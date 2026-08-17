import { cn } from '@/lib/cn'
import { fmtDate } from '@/lib/format'

export interface DateShortcutOption {
  label: string
  /** 相对基准日期 base 的天数偏移 (0=base, 缺省今天) */
  days: number
}

interface Props {
  /** 当前值 (YYYY-MM-DD), 用于高亮命中的快捷项 */
  value: string
  onChange: (v: string) => void
  options: DateShortcutOption[]
  /** 基准日期 (YYYY-MM-DD), 缺省今天; 如「到期日 = 买入日期 + N 天」 */
  base?: string
}

function addDaysISO(days: number, base?: string): string {
  const d = base ? new Date(`${base}T00:00:00`) : new Date()
  d.setDate(d.getDate() + days)
  return fmtDate(d)
}

/**
 * 快捷日期 chips — 与 DatePicker 并存: 快捷走 chips, 精确日期走日历。
 */
export function DateShortcuts({ value, onChange, options, base }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {options.map(opt => {
        const target = addDaysISO(opt.days, base)
        const active = value === target
        return (
          <button
            key={opt.label}
            type="button"
            title={target}
            onClick={() => onChange(active ? '' : target)}
            className={cn(
              'rounded px-1.5 py-0.5 text-[10px] transition-colors cursor-pointer',
              active
                ? 'bg-accent/15 text-accent border border-accent/30'
                : 'bg-elevated text-muted border border-transparent hover:text-foreground',
            )}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
