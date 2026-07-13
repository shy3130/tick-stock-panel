import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'
import { NAV_GROUP_COLOR, type NavGroup, type NavIconComponent } from '@/lib/navRegistry'

interface Props {
  title: string
  subtitle?: string
  titleExtra?: ReactNode
  right?: ReactNode
  rightClassName?: string
  className?: string
  icon?: NavIconComponent
  group?: NavGroup
}

export function PageHeader({ title, subtitle, titleExtra, right, rightClassName, className, icon: Icon, group }: Props) {
  const groupColor = group ? NAV_GROUP_COLOR[group] : undefined
  return (
    <header
      className={cn(
        'flex flex-col items-stretch justify-between gap-2 border-b border-border px-3 pb-2 pt-3 lg:flex-row lg:items-center lg:gap-4 lg:px-5',
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        {Icon && groupColor && (
          <span
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
            style={{ backgroundColor: `color-mix(in srgb, ${groupColor} 15%, transparent)`, color: groupColor }}
          >
            <Icon className="h-4 w-4" />
          </span>
        )}
        <h1 className="shrink-0 whitespace-nowrap text-lg font-semibold tracking-tight">{title}</h1>
        {titleExtra}
        {subtitle && <span className="min-w-0 basis-full text-xs text-muted xl:basis-auto">{subtitle}</span>}
      </div>
      {right && <div className={cn('w-full min-w-0 overflow-x-auto lg:w-auto lg:shrink-0', rightClassName)}>{right}</div>}
    </header>
  )
}
