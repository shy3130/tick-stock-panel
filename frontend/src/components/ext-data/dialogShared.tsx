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
