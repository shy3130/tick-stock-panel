import { cn } from '@/lib/cn'

interface Props {
  title: string
  subtitle?: string
  /** 标题右侧、subtitle 之前的额外节点(如状态徽标) */
  titleExtra?: React.ReactNode
  right?: React.ReactNode
  className?: string
}

export function PageHeader({ title, subtitle, titleExtra, right, className }: Props) {
  return (
    <header
      className={cn(
        'flex flex-col gap-2 border-b border-border px-3 pb-2 pt-3 sm:px-5 md:flex-row md:items-center md:justify-between md:gap-4',
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {titleExtra}
        {subtitle && <span className="text-xs text-muted">{subtitle}</span>}
      </div>
      {right && (
        <div aria-label="页面操作" className="w-full overflow-x-auto pb-0.5 md:w-auto md:overflow-visible md:pb-0">
          {right}
        </div>
      )}
    </header>
  )
}
