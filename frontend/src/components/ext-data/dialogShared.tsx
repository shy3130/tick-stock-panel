import type { ReactNode } from 'react'

/** 扩展数据弹窗共用的分节标题: mono 序号 + 标题 + 发丝线 + 右侧动作区 */
export function SectionHead({ num, title, children }: {
  num: string
  title: string
  children?: ReactNode
}) {
  return (
    <div className="flex items-center gap-2.5 mb-2.5">
      <span className="font-mono text-[10px] text-amber-500/80 tabular-nums">{num}</span>
      <span className="text-xs font-semibold text-foreground shrink-0">{title}</span>
      <div className="flex-1 h-px bg-border/60" />
      {children}
    </div>
  )
}

export const INPUT_CLS = 'w-full h-9 px-3 rounded-lg bg-base border border-border text-xs text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/30 focus:border-accent/50 transition-shadow'
export const INPUT_SM_CLS = 'w-full h-8 px-2.5 rounded-lg border border-border bg-base text-[11px] text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow'

export const MIN_PULL_INTERVAL_SECONDS = 10
export const MAX_PULL_INTERVAL_SECONDS = 2_592_000

/** 与 PullConfigReq 的整数范围一致，必须在建表或保存凭据前校验。 */
export function validatePullInterval(value: number, unit: 'seconds' | 'minutes'): string | null {
  if (unit === 'seconds') {
    return Number.isInteger(value) && value >= MIN_PULL_INTERVAL_SECONDS && value <= MAX_PULL_INTERVAL_SECONDS
      ? null : `秒级拉取间隔须为 ${MIN_PULL_INTERVAL_SECONDS}~${MAX_PULL_INTERVAL_SECONDS} 的整数（最多 30 天）`
  }
  return Number.isInteger(value) && value >= 1 ? null : '分钟拉取间隔须为大于等于 1 的整数'
}

/** 间隔摘要 (与后端 _interval_seconds 同口径): schedule_seconds 优先, 回退分钟。
 *  <60s 或不能整分 → 「每 N 秒」; 整分 → 「每 N 分钟」 */
export function formatIntervalSummary(
  seconds: number | null | undefined, minutes: number | null | undefined,
): string {
  const secs = seconds ?? (minutes ?? 1440) * 60
  return secs >= 60 && secs % 60 === 0 ? `每 ${secs / 60} 分钟` : `每 ${secs} 秒`
}
