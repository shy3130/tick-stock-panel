import { useState, useRef, useEffect } from 'react'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { Calendar, ChevronLeft, ChevronRight } from 'lucide-react'

interface DatePickerProps {
  value: string          // YYYY-MM-DD
  onChange: (v: string) => void
  min?: string
  max?: string
  placeholder?: string
  className?: string
  buttonClassName?: string
  align?: 'left' | 'right'
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
}: DatePickerProps) {
  const [open, setOpen] = useState(false)
  const [showYearPicker, setShowYearPicker] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()

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
    const closeOnEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        setShowYearPicker(false)
      }
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('keydown', closeOnEscape)
    }
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
    cells.push({ day: d, cur: false, dateStr: ds, disabled: !!min && ds < min || !!max && ds > max })
  }
  // 当月
  for (let d = 1; d <= daysInMonth; d++) {
    const ds = toDateStr(viewYear, viewMonth, d)
    cells.push({ day: d, cur: true, dateStr: ds, disabled: !!min && ds < min || !!max && ds > max })
  }
  // 下月头部 — 补齐到 6 行 × 7 = 42
  const remain = 42 - cells.length
  for (let d = 1; d <= remain; d++) {
    const m = viewMonth === 11 ? 0 : viewMonth + 1
    const y = viewMonth === 11 ? viewYear + 1 : viewYear
    const ds = toDateStr(y, m, d)
    cells.push({ day: d, cur: false, dateStr: ds, disabled: !!min && ds < min || !!max && ds > max })
  }

  const displayLabel = value || placeholder
  const today = todayStr()

  return (
    <div ref={ref} className={`relative inline-flex ${className}`}>
      {/* 触发按钮 */}
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={value ? `选择日期，当前 ${value}` : placeholder}
        className={`inline-flex h-7 items-center gap-1.5 whitespace-nowrap px-2.5 rounded-input border border-border
          bg-elevated hover:border-accent/50 text-xs text-foreground num
          focus:outline-none focus:border-accent/60 transition-colors duration-150 cursor-pointer ${buttonClassName}`}
      >
        <Calendar className="h-3.5 w-3.5 text-accent" />
        <span className={value ? undefined : 'text-muted'}>{displayLabel}</span>
      </button>

      {/* 弹出日历 */}
      <AnimatePresence>
        {open && (
          <motion.div
            initial={reduceMotion ? false : { opacity: 0, y: -4, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduceMotion ? undefined : { opacity: 0, y: -4, scale: 0.97 }}
            transition={reduceMotion ? { duration: 0 } : { duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className={`absolute ${align === 'left' ? 'left-0' : 'right-0'} top-full mt-1.5 z-50 w-[260px] rounded-card border border-border
              bg-surface shadow-[0_8px_30px_rgba(0,0,0,0.4)] p-3`}
            role="dialog"
            aria-label="选择日期"
          >
            {/* 月份导航 */}
            <div className="flex items-center justify-between mb-2">
              <button
                type="button"
                onClick={showYearPicker ? () => setViewYear(viewYear - 12) : prevMonth}
                aria-label={showYearPicker ? '前 12 年' : '上个月'}
                className="p-1 rounded-btn hover:bg-elevated text-secondary hover:text-foreground transition-colors"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => setShowYearPicker(v => !v)}
                aria-label={showYearPicker ? '返回月份选择' : '选择年份'}
                className="text-sm font-medium text-foreground num hover:text-accent transition-colors cursor-pointer"
              >
                {showYearPicker
                  ? `${viewYear - 5} - ${viewYear + 6}`
                  : `${viewYear} 年 ${viewMonth + 1} 月`
                }
              </button>
              <button
                type="button"
                onClick={showYearPicker ? () => setViewYear(viewYear + 12) : nextMonth}
                aria-label={showYearPicker ? '后 12 年' : '下个月'}
                className="p-1 rounded-btn hover:bg-elevated text-secondary hover:text-foreground transition-colors"
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
                      className={`h-8 text-xs rounded-btn transition-colors duration-100
                        ${isSelected ? 'bg-accent text-white font-bold dark:text-[hsl(var(--base))]' : ''}
                        ${isThisYear && !isSelected ? 'border border-accent/40' : ''}
                        ${!isSelected ? 'hover:bg-elevated cursor-pointer text-foreground' : ''}
                      `}
                    >
                      {y}
                    </button>
                  )
                })}
              </div>
            ) : (
              <>
                {/* 星期头 */}
                <div className="grid grid-cols-7 text-center text-[10px] text-muted mb-1">
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
                    aria-label={c.dateStr}
                    aria-pressed={isSelected}
                    onClick={() => {
                      if (!c.disabled) {
                        onChange(c.dateStr)
                        setOpen(false)
                      }
                    }}
                    className={`
                      h-7 w-full text-xs rounded-btn transition-colors duration-100
                      ${c.cur ? 'text-foreground' : 'text-muted/40'}
                      ${isSelected ? 'bg-accent text-white font-bold dark:text-[hsl(var(--base))]' : ''}
                      ${isToday && !isSelected ? 'border border-accent/40' : ''}
                      ${!isSelected && !c.disabled ? 'hover:bg-elevated' : ''}
                      ${c.disabled ? 'opacity-20 cursor-not-allowed' : 'cursor-pointer'}
                    `}
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
