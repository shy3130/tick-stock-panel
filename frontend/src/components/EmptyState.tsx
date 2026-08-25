import { type LucideIcon, Construction } from 'lucide-react'
import { cn } from '@/lib/cn'

interface Props {
  icon?: LucideIcon
  title: string
  hint?: string
  className?: string
}

// §6.0.5 四态评审 — empty 状态:图示 + 引导,而不是一句"暂无数据"
export function EmptyState({ icon: Icon = Construction, title, hint, className }: Props) {
  return (
    <div className={cn('grid h-full place-items-center px-8 py-16', className)}>
      <div className="max-w-md text-center">
        <Icon className="mx-auto h-10 w-10 text-muted" strokeWidth={1.5} aria-hidden />
        <h2 className="mt-4 text-base font-medium text-foreground">{title}</h2>
        {hint && <p className="mt-2 text-sm leading-relaxed text-secondary">{hint}</p>}
      </div>
    </div>
  )
}
