import { type LucideIcon, Construction } from 'lucide-react'

interface Props {
  icon?: LucideIcon
  title: string
  hint?: string
}

// §6.0.5 四态评审 — empty 状态:图示 + 引导,而不是一句"暂无数据"
export function EmptyState({ icon: Icon = Construction, title, hint }: Props) {
  return (
    <div className="grid h-full min-w-0 place-items-center px-4 py-10 sm:px-8 sm:py-16">
      <div className="w-full max-w-md min-w-0 text-center">
        <Icon className="mx-auto h-9 w-9 text-muted sm:h-10 sm:w-10" strokeWidth={1.5} />
        <h2 className="mt-3 text-sm font-medium leading-6 text-foreground sm:mt-4 sm:text-base">{title}</h2>
        {hint && <p className="mt-2 break-words text-xs leading-5 text-secondary sm:text-sm sm:leading-relaxed">{hint}</p>}
      </div>
    </div>
  )
}
