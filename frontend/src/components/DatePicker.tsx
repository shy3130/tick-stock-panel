import { useState, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Calendar, ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/cn'

interface DatePickerProps {
  value: string          // YYYY-MM-DD
  onChange: (v: string) => void
  min?: string
  max?: string
  placeholder?: string
  className?: string
  buttonClassName?: string
  align?: 'left' | 'right'
  /** 额外的禁用判定 (如：仅允许交易日)，与 min/max 叠加 */
  isDisabledDate?: (dateStr: string) => boolean
}

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

function pad(n: number) { return String(n).padStart(2, '0') }
function toDateStr(y: number, m: number, d: number) {
  return `${y}-${pad(m + 1)}-${pad(d)}`
}
function todayStr() {
  const date = new Date()
  return toDateStr(date.getFullYear(), date.getMonth(), date.getDate())
}
function viewDate(value: string, min?: string, max?: string) {
  const source = value || max || min || todayStr()
  return {
    year: Number(source.slice(0, 4)),
    month: Number(source.slice(5, 7)) - 1,
  }
}

export function DatePicker({
  value,
  onChange,
  min,
  max,
  placeholder = '选择日期',
  className = '',
  buttonClassName = '',
  align = 'right',
  isDisabledDate,
}: DatePickerProps) {
  const [open, setOpen] = useState(false)
  const [showYearPicker, setShowYearPicker] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 当前显示的月份
  const [viewYear, setViewYear] = useState(() => viewDate(value, min, max).year)
  const [viewMonth, setViewMonth] = useState(() => viewDate(value, min, max).month)

  // 当 value 外部变化时同步 view
  useEffect(() => {
    const next = viewDate(value, min, max)
    setViewYear(next.year)
    setViewMonth(next.month)
  }, [value, min, max])

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const prevMonth = () => {
    if (viewMonth === 0) { setViewMonth(11); setViewYear(viewYear - 1) }
    else setViewMonth(viewMonth - 1)
  }
  const nextMonth = () => {
    if (viewMonth === 11) { setViewMonth(0); setViewYear(viewYear + 1) }
    else setViewMonth(viewMonth + 1)
  }

  // 构建日历格子: 周一为第一天
  const firstDay = new Date(viewYear, viewMonth, 1).getDay()
  const offset = firstDay === 0 ? 6 : firstDay - 1          // 周一=0
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate()
  const prevMonthDays = new Date(viewYear, viewMonth, 0).getDate()

  const cells: { day: number; cur: boolean; dateStr: string; disabled: boolean }[] = []

  // 上月尾部
  for (let i = offset - 1; i >= 0; i--) {
    const d = prevMonthDays - i
    const m = viewMonth === 0 ? 11 : viewMonth - 1
    const y = viewMonth === 0 ? viewYear - 1 : viewYear
    const ds = toDateStr(y, m, d)
    cells.push({ day: d, cur: false, dateStr: ds, disabled: !!min && ds < min || !!max && ds > max || (isDisabledDate?.(ds) ?? false) })
  }
  // 当月
  for (let d = 1; d <= daysInMonth; d++) {
    const ds = toDateStr(viewYear, viewMonth, d)
    cells.push({ day: d, cur: true, dateStr: ds, disabled: !!min && ds < min || !!max && ds > max || (isDisabledDate?.(ds) ?? false) })
  }
  // 下月头部 — 补齐到 6 行 × 7 = 42
  const remain = 42 - cells.length
  for (let d = 1; d <= remain; d++) {
    const m = viewMonth === 11 ? 0 : viewMonth + 1
    const y = viewMonth === 11 ? viewYear + 1 : viewYear
    const ds = toDateStr(y, m, d)
    cells.push({ day: d, cur: false, dateStr: ds, disabled: !!min && ds < min || !!max && ds > max || (isDisabledDate?.(ds) ?? false) })
  }

  const displayLabel = value || placeholder
  const today = todayStr()

  return (
    <div ref={ref} className={cn('relative inline-flex', className)}>
      {/* 触发按钮 */}
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className={cn(
          'control h-7 cursor-pointer px-2.5 text-xs num',
          'hover:border-accent/50 focus:border-accent/60',
          buttonClassName,
        )}
      >
        <Calendar className="h-3.5 w-3.5 text-accent" />
        <span className={value ? undefined : 'text-muted'}>{displayLabel}</span>
      </button>

      {/* 弹出日历 */}
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.97 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              'panel absolute top-full z-50 mt-1.5 w-[260px] p-3 shadow-lg',
              align === 'left' ? 'left-0' : 'right-0',
            )}
          >
            {/* 月份导航 */}
            <div className="mb-2 flex items-center justify-between">
              <button
                type="button"
                onClick={showYearPicker ? () => setViewYear(viewYear - 12) : prevMonth}
                className="btn-ghost h-auto p-1 text-secondary hover:text-foreground"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => setShowYearPicker(v => !v)}
                className="cursor-pointer text-sm font-medium text-foreground num transition-colors hover:text-accent"
              >
                {showYearPicker
                  ? `${viewYear - 5} - ${viewYear + 6}`
                  : `${viewYear} 年 ${viewMonth + 1} 月`
                }
              </button>
              <button
                type="button"
                onClick={showYearPicker ? () => setViewYear(viewYear + 12) : nextMonth}
                className="btn-ghost h-auto p-1 text-secondary hover:text-foreground"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>

            {showYearPicker ? (
              /* 年份选择网格 */
              <div className="grid grid-cols-4 gap-1">
                {Array.from({ length: 12 }, (_, i) => viewYear - 5 + i).map(y => {
                  const isSelected = y === Number(value.slice(0, 4))
                  const isThisYear = y === new Date().getFullYear()
                  return (
                    <button
                      key={y}
                      type="button"
                      onClick={() => {
                        setViewYear(y)
                        setShowYearPicker(false)
                      }}
                      className={cn(
                        'h-8 rounded-btn text-xs transition-colors duration-100',
                        isSelected && 'bg-accent font-bold text-white',
                        isThisYear && !isSelected && 'border border-accent/40',
                        !isSelected && 'cursor-pointer text-foreground hover:bg-elevated',
                      )}
                    >
                      {y}
                    </button>
                  )
                })}
              </div>
            ) : (
              <>
                {/* 星期头 */}
                <div className="mb-1 grid grid-cols-7 text-center text-[10px] text-muted">
                  {WEEKDAYS.map((w) => (
                    <div key={w}>{w}</div>
                  ))}
                </div>

                {/* 日期格子 */}
                <div className="grid grid-cols-7 gap-px">
                  {cells.map((c, i) => {
                    const isSelected = c.dateStr === value
                    const isToday = c.dateStr === today
                    return (
                      <button
                        key={i}
                        type="button"
                        disabled={c.disabled}
                        onClick={() => {
                          if (!c.disabled) {
                            onChange(c.dateStr)
                            setOpen(false)
                          }
                        }}
                        className={cn(
                          'h-7 w-full rounded-btn text-xs transition-colors duration-100',
                          c.cur ? 'text-foreground' : 'text-muted/40',
                          isSelected && 'bg-accent font-bold text-white',
                          isToday && !isSelected && 'border border-accent/40',
                          !isSelected && !c.disabled && 'hover:bg-elevated',
                          c.disabled ? 'cursor-not-allowed opacity-20' : 'cursor-pointer',
                        )}
                      >
                        {c.day}
                      </button>
                    )
                  })}
                </div>
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
