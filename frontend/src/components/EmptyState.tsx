import { type LucideIcon, Construction } from 'lucide-react'

interface Props {
  icon?: LucideIcon
  title: string
  hint?: string
}

// §6.0.5 四态评审 — empty 状态:图示 + 引导,而不是一句"暂无数据"
export function EmptyState({ icon: Icon = Construction, title, hint }: Props) {
  return (
    <div className="h-full grid place-items-center px-8 py-16">
      <div className="text-center max-w-md">
        <Icon className="mx-auto h-10 w-10 text-muted" strokeWidth={1.5} />
        <h2 className="mt-4 text-base font-medium text-foreground">{title}</h2>
        {hint && <p className="mt-2 text-sm text-secondary leading-relaxed">{hint}</p>}
      </div>
    </div>
  )
}
