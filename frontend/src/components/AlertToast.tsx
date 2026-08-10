import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Bell, TrendingUp, TrendingDown, X } from 'lucide-react'
import type { AlertEvent } from '@/lib/api'
import { fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/cn'
import { playNotificationSound } from '@/lib/notificationSound'
import { SOLID_TONE, TEXT_TONE } from '@/components/ui/Primitives'

// ===== 全局状态 (模块级, 仿 Toast.tsx 模式) =====
type Item = { id: number; alert: AlertEvent }
let _id = 0
let _queue: Item[] = []
const AUTO_DISMISS = 5000      // 5 秒自动消失
const _listeners: Set<(items: Item[]) => void> = new Set()

/** 从 localStorage 读取配置 */
function getEnabled(): boolean {
  try {
    const v = localStorage.getItem('alert_toast_enabled')
    return v === null ? true : v === '1'   // 默认开启
  } catch { return true }
}

function getMaxVisible(): number {
  try {
    const v = parseInt(localStorage.getItem('alert_toast_max') || '', 10)
    return v >= 1 && v <= 10 ? v : 3       // 默认 3, 范围 1-10
  } catch { return 3 }
}

/** 通知外部配置变更后刷新 (设置页改了配置后调用) */
export function refreshAlertToastConfig() {
  _emit()
}

function _emit() { _listeners.forEach(fn => fn([..._queue])) }

/** 推入单条监控告警通知 (兼容入口, 不发声 — 发声由批量入口统一处理) */
export function pushAlertToast(alert: AlertEvent) {
  pushAlertToasts([alert])
}

/**
 * 批量推入监控告警通知 (一轮 SSE 多只新命中时调用)。
 * - 每条都弹 Toast (受 maxVisible 上限, 超出丢最旧)
 * - 整批只播放一声通知音, 避免短时连续响多声刷屏
 */
export function pushAlertToasts(alerts: AlertEvent[]) {
  if (alerts.length === 0) return
  if (!getEnabled()) return                  // 开关关闭: 不弹
  const maxVisible = getMaxVisible()
  const newItems = alerts.map(alert => ({ id: ++_id, alert }))
  _queue = [..._queue, ...newItems]
  // 超出上限: 丢弃最旧的
  if (_queue.length > maxVisible) {
    _queue = _queue.slice(-maxVisible)
  }
  _emit()
  for (const item of newItems) {
    setTimeout(() => dismiss(item.id), AUTO_DISMISS)
  }
  playNotificationSound()                     // 整批只响一声
}

/** 手动关闭 */
export function dismiss(id: number) {
  _queue = _queue.filter(t => t.id !== id)
  _emit()
}

// ===== 配色 — 全部静态 semantic token =====
const SEVERITY_BAR: Record<string, string> = {
  info: SOLID_TONE.accent,
  warn: SOLID_TONE.warning,
  critical: SOLID_TONE.danger,
}

const SEVERITY_ICON: Record<string, string> = {
  info: TEXT_TONE.accent,
  warn: TEXT_TONE.warning,
  critical: TEXT_TONE.danger,
}

const SOURCE_BADGE: Record<string, { label: string; cls: string }> = {
  strategy:  { label: '策略', cls: 'bg-accent/15 text-accent' },
  signal:    { label: '信号', cls: 'bg-info/15 text-info' },
  price:     { label: '价格', cls: 'bg-success/15 text-success' },
  market:    { label: '异动', cls: 'bg-warning/15 text-warning' },
  new_entry: { label: '进入', cls: 'bg-success/15 text-success' },
  dropped:   { label: '移出', cls: 'bg-danger/15 text-danger' },
}

// ===== 容器 — 挂在 Layout =====
export function AlertToastContainer() {
  const [items, setItems] = useState<Item[]>([])
  const navigate = useNavigate()

  const sub = useCallback(() => {
    _listeners.add(setItems)
    return () => { _listeners.delete(setItems) }
  }, [])
  useEffect(sub, [sub])

  // 点击通知 → 跳转监控中心 + 关闭当前通知
  const handleClick = (id: number) => {
    dismiss(id)
    navigate('/monitor')
  }

  if (!items.length) return null

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[9999] flex w-[320px] flex-col gap-2">
      <AnimatePresence>
        {items
          .filter(item => !(item.alert.source === 'strategy' && !item.alert.symbol))
          .map(item => {
          const ev = item.alert
          const sevKey = ev.severity ?? 'info'
          const sev = SEVERITY_BAR[sevKey] ?? SEVERITY_BAR.info
          const sevIcon = SEVERITY_ICON[sevKey] ?? SEVERITY_ICON.info
          const badgeKey = (ev.source === 'strategy' && ev.type) ? ev.type : ev.source
          const badge = SOURCE_BADGE[badgeKey] ?? { label: badgeKey, cls: 'bg-elevated text-muted' }
          const pct = ev.change_pct ?? 0
          const isStrategy = ev.source === 'strategy'
          const sm = isStrategy ? ev.message?.match(/策略「([^」]+)」/) : null
          const sname = sm ? sm[1] : ''
          const isNew = ev.type === 'new_entry'
          return (
            <motion.div
              key={item.id}
              layout
              initial={{ opacity: 0, x: 60, scale: 0.9 }}
              animate={{ opacity: 1, x: 0, scale: 1 }}
              exit={{ opacity: 0, x: 60, scale: 0.9 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              onClick={() => handleClick(item.id)}
              className="pointer-events-auto relative cursor-pointer overflow-hidden rounded-xl border border-border bg-surface py-2.5 pl-3 pr-2 shadow-lg transition-colors hover:border-accent/40"
            >
              {/* 左侧色条 */}
              <div className={cn('absolute left-0 top-0 h-full w-0.5', sev)} />

              {/* 顶行: 分类标签 + 代码/名称 + 涨跌幅 + 关闭 */}
              <div className="flex items-center gap-2">
                <span className={cn('shrink-0 rounded px-1 py-px text-[9px] font-medium', badge.cls)}>
                  {badge.label}
                </span>
                {ev.symbol && <span className="shrink-0 font-mono text-xs font-medium text-foreground">{ev.symbol}</span>}
                {ev.name && <span className="flex-1 truncate text-xs text-secondary">{ev.name}</span>}
                {ev.change_pct != null && (
                  <span className={cn(
                    'inline-flex shrink-0 items-center gap-0.5 font-mono text-[10px] font-medium',
                    pct >= 0 ? 'text-bull' : 'text-bear',
                  )}>
                    {pct >= 0 ? <TrendingUp className="h-2.5 w-2.5" /> : <TrendingDown className="h-2.5 w-2.5" />}
                    {fmtPct(pct)}
                  </span>
                )}
                <button
                  onClick={(e) => { e.stopPropagation(); dismiss(item.id) }}
                  className="shrink-0 cursor-pointer rounded p-0.5 text-muted/50 transition-colors hover:bg-elevated hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>

              {/* 底行: 策略类型走新格式, 其他走旧格式 */}
              {isStrategy ? (
                <div className="mt-1 flex items-center gap-1.5 pl-0.5">
                  <Bell className={cn('h-3 w-3 shrink-0', sevIcon)} />
                  <span className={cn('text-[11px] font-medium', isNew ? 'text-bull' : 'text-bear')}>
                    {isNew ? '进入' : '移出'}
                  </span>
                  <span className="text-[11px] text-secondary">策略</span>
                  <span className="text-[11px] font-medium text-foreground">「{sname}」</span>
                  <span className="flex-1" />
                  {ev.price != null && <span className="shrink-0 font-mono text-[10px] text-muted">{fmtPrice(ev.price)}</span>}
                </div>
              ) : (
                <div className="mt-1 flex items-center gap-1.5 pl-0.5">
                  <Bell className={cn('h-3 w-3 shrink-0', sevIcon)} />
                  {/* message 已含「条件摘要 · 现价 · 涨跌幅」(后端生成), 直接展示避免重复 */}
                  {ev.message && <span className="flex-1 truncate text-[11px] text-secondary">{ev.message}</span>}
                </div>
              )}
            </motion.div>
          )
        })}
      </AnimatePresence>
    </div>
  )
}
