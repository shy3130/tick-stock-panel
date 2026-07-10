import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'
import { NAV_GROUP_COLOR, type NavGroup, type NavIconComponent } from '@/lib/navRegistry'

interface Props {
  title: string
  subtitle?: string
  titleExtra?: ReactNode
  right?: ReactNode
  className?: string
  icon?: NavIconComponent
  group?: NavGroup
}

export function PageHeader({ title, subtitle, titleExtra, right, className, icon: Icon, group }: Props) {
  const groupColor = group ? NAV_GROUP_COLOR[group] : undefined
  return (
    <header
      className={cn(
        'px-5 pt-3 pb-2 border-b border-border flex items-center justify-between gap-4',
        className,
      )}
    >
      <div className="flex items-center gap-2">
        {Icon && groupColor && (
          <span
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
            style={{ backgroundColor: `color-mix(in srgb, ${groupColor} 15%, transparent)`, color: groupColor }}
          >
            <Icon className="h-4 w-4" />
          </span>
        )}
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {titleExtra}
        {subtitle && <span className="text-xs text-muted">{subtitle}</span>}
      </div>
      {right}
    </header>
  )
}
