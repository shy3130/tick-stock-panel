import { cn } from '@/lib/cn'

interface Props {
  title: string
  subtitle?: string
  /** 标题右侧、subtitle 之前的额外节点(如状态徽标) */
  titleExtra?: React.ReactNode
  right?: React.ReactNode
  className?: string
}

/**
 * Workspace page title bar.
 * Props API is stable — callers need not change.
 */
export function PageHeader({ title, subtitle, titleExtra, right, className }: Props) {
  return (
    <header
      className={cn(
        'flex min-w-0 flex-col gap-2 border-b border-border bg-surface/80 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:px-4',
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <div className="flex min-w-0 items-center gap-2">
          <h1 className="truncate text-base font-semibold tracking-tight text-foreground sm:text-lg">
            {title}
          </h1>
          {titleExtra}
        </div>
        {subtitle ? (
          <span className="truncate text-xs text-muted sm:text-[13px]">{subtitle}</span>
        ) : null}
      </div>
      {right ? (
        <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">
          {right}
        </div>
      ) : null}
    </header>
  )
}
