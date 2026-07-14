import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Bell, TrendingUp, TrendingDown, X } from 'lucide-react'
import type { AlertEvent } from '@/lib/api'
import { fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/cn'
import { playNotificationSound } from '@/lib/notificationSound'
import { speakAlerts } from '@/lib/voiceBroadcast'
import { usePreferences } from '@/lib/useSharedQueries'

/** 通知渠道分发 — 所有副作用渠道在此汇合, 新增渠道只改这里 */
function dispatchSideEffects(alerts: AlertEvent[]) {
  playNotificationSound()        // 提示音 (Web Audio 合成)
  speakAlerts(alerts)            // 语音播报 (speechSynthesis, 各自独立开关)
}

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
  dispatchSideEffects(alerts)                  // 副作用分发: 提示音 + 语音 (整批各一次)
}

/** 手动关闭 */
export function dismiss(id: number) {
  _queue = _queue.filter(t => t.id !== id)
  _emit()
}

// ===== 配色 =====
const SEVERITY_BAR: Record<string, string> = {
  info: 'bg-accent', warn: 'bg-warning', critical: 'bg-danger',
}
const SOURCE_BADGE: Record<string, { label: string; cls: string }> = {
  strategy:  { label: '策略',   cls: 'bg-amber-400/15 text-amber-400' },
  signal:    { label: '信号',   cls: 'bg-accent/15 text-accent' },
  price:     { label: '价格',   cls: 'bg-emerald-400/15 text-emerald-400' },
  market:    { label: '异动',   cls: 'bg-purple-500/15 text-purple-400' },
  new_entry: { label: '进入',   cls: 'bg-emerald-400/15 text-emerald-400' },
  dropped:   { label: '移出', cls: 'bg-danger/15 text-danger' },
}

// ===== 容器 — 挂在 Layout =====
export function AlertToastContainer() {
  const [items, setItems] = useState<Item[]>([])
  const navigate = useNavigate()
  const { data: prefs } = usePreferences()
  const extFields = prefs?.monitor_ext_fields ?? {
    concept: { field: 'ext_gn_ths.所属概念' },
    industry: { field: 'ext_hy_ths.所属同花顺行业' },
  }

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
    <div
      role="status"
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-4 right-4 z-[9999] flex flex-col gap-2 w-[320px] pointer-events-none"
    >
      <AnimatePresence>
        {items
          .filter(item => !(item.alert.source === 'strategy' && !item.alert.symbol))
          .map(item => {
          const ev = item.alert
          const sev = SEVERITY_BAR[ev.severity ?? 'info'] ?? SEVERITY_BAR.info
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
              role="button"
              tabIndex={0}
              aria-label={`查看监控通知${ev.name ? ` ${ev.name}` : ''}${ev.symbol ? ` ${ev.symbol}` : ''}`}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  handleClick(item.id)
                }
              }}
              className="pointer-events-auto relative overflow-hidden rounded-xl border border-border/60 bg-surface/95 backdrop-blur-md shadow-2xl pl-3 pr-2 py-2.5 cursor-pointer hover:border-accent/40 hover:shadow-accent/10 transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
            >
              {/* 左侧色条 */}
              <div className={cn('absolute left-0 top-0 h-full w-0.5', sev)} />

              {/* 顶行: 分类标签 + 代码/名称 + 涨跌幅 + 关闭 */}
              <div className="flex items-center gap-2">
                <span className={cn('shrink-0 rounded px-1 py-px text-[9px] font-medium', badge.cls)}>
                  {badge.label}
                </span>
                {ev.symbol && <span className="font-mono text-xs font-medium text-foreground shrink-0">{ev.symbol}</span>}
                {ev.name && <span className="text-xs text-secondary truncate flex-1">{ev.name}</span>}
                {ev.change_pct != null && (
                  <span className={cn('inline-flex items-center gap-0.5 text-[10px] font-mono font-medium shrink-0', pct >= 0 ? 'text-danger' : 'text-bear')}>
                    {pct >= 0 ? <TrendingUp className="h-2.5 w-2.5" /> : <TrendingDown className="h-2.5 w-2.5" />}
                    {fmtPct(pct)}
                  </span>
                )}
                <button aria-label="关闭通知" onClick={(e) => { e.stopPropagation(); dismiss(item.id) }} className="shrink-0 p-0.5 rounded text-muted/50 hover:text-foreground hover:bg-elevated transition-colors cursor-pointer">
                  <X className="h-3 w-3" />
                </button>
              </div>

              {/* 底行: 策略类型走新格式, 其他走旧格式 */}
              {isStrategy ? (
                <div className="mt-1 flex items-center gap-1.5 pl-0.5">
                  <Bell className={cn('h-3 w-3 shrink-0', sev.replace('bg-', 'text-'))} />
                  <span className={cn('text-[11px] font-medium', isNew ? 'text-danger' : 'text-emerald-400')}>
                    {isNew ? '进入' : '移出'}
                  </span>
                  <span className="text-[11px] text-foreground/70">策略</span>
                  <span className="text-[11px] font-medium text-amber-400">「{sname}」</span>
                  <span className="flex-1" />
                  {ev.price != null && <span className="text-[10px] font-mono text-muted shrink-0">{fmtPrice(ev.price)}</span>}
                </div>
              ) : (
                <div className="mt-1 flex items-center gap-1.5 pl-0.5">
                  <Bell className={cn('h-3 w-3 shrink-0', sev.replace('bg-', 'text-'))} />
                  {/* message 已含「条件摘要 · 现价 · 涨跌幅」(后端生成), 直接展示避免重复 */}
                  {ev.message && <span className="text-[11px] text-foreground/70 truncate flex-1">{ev.message}</span>}
                </div>
              )}

              {/* 行业/概念标签 (后端 SSE 推送时已富化, 字段配置来自监控中心全局设置) */}
              {(() => {
                const tags: { text: string; cls: string }[] = []
                for (const [isIndustry, item] of [[true, extFields.industry], [false, extFields.concept]] as const) {
                  if (!item?.field) continue
                  const key = item.field.replace('.', '__')
                  const v = (ev as Record<string, unknown>)[key]
                  if (v == null) continue
                  let parts = String(v).split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)
                  const mt = item.maxTags ?? 0
                  if (mt > 0) parts = parts.slice(0, mt)
                  const hi = item.hiddenIndices
                  if (hi?.length) parts = parts.filter((_, i) => !hi.includes(i))
                  for (const t of parts) {
                    tags.push({ text: t, cls: isIndustry ? 'bg-sky-500/10 text-sky-400' : 'bg-orange-500/10 text-orange-400' })
                  }
                }
                if (!tags.length) return null
                return (
                  <div className="mt-1 flex flex-wrap items-center gap-1 pl-0.5">
                    {tags.map((t, i) => (
                      <span key={i} className={cn('rounded px-1 py-px text-[9px] leading-tight', t.cls)}>{t.text}</span>
                    ))}
                  </div>
                )
              })()}
            </motion.div>
          )
        })}
      </AnimatePresence>
    </div>
  )
}
