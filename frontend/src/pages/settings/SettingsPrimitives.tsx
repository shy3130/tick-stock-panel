import { useId, type ComponentType, type ReactNode } from 'react'
import { cn } from '@/lib/cn'

type SettingsIcon = ComponentType<{ className?: string }>

const PANEL_WIDTH = {
  narrow: 'max-w-2xl',
  default: 'max-w-5xl',
  wide: 'max-w-6xl',
  full: 'max-w-none',
} as const

interface SettingsPanelProps {
  icon: SettingsIcon
  title: string
  description: string
  action?: ReactNode
  width?: keyof typeof PANEL_WIDTH
  className?: string
  children: ReactNode
}

export function SettingsPanel({
  icon: Icon,
  title,
  description,
  action,
  width = 'default',
  className,
  children,
}: SettingsPanelProps) {
  return (
    <div className={cn('min-w-0 space-y-5', PANEL_WIDTH[width], className)}>
      <header className="flex flex-col gap-3 border-b border-border/80 pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-accent/15 bg-accent/10 text-accent">
            <Icon className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-foreground">{title}</h2>
            <p className="mt-0.5 text-xs leading-5 text-muted">{description}</p>
          </div>
        </div>
        {action && <div className="shrink-0 self-start sm:self-auto">{action}</div>}
      </header>
      {children}
    </div>
  )
}

interface SettingsSectionProps {
  icon?: SettingsIcon
  title: string
  description?: string
  badge?: ReactNode
  action?: ReactNode
  flush?: boolean
  unframed?: boolean
  className?: string
  contentClassName?: string
  children: ReactNode
}

export function SettingsSection({
  icon: Icon,
  title,
  description,
  badge,
  action,
  flush = false,
  unframed = false,
  className,
  contentClassName,
  children,
}: SettingsSectionProps) {
  return (
    <section className={cn(!unframed && 'overflow-hidden rounded-card border border-border bg-surface', className)}>
      <header className={cn(
        'flex min-h-12 items-center justify-between gap-3 border-b border-border/70',
        unframed ? 'pb-3' : 'px-4 py-3',
      )}>
        <div className="flex min-w-0 items-center gap-2.5">
          {Icon && (
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-accent/10 text-accent">
              <Icon className="h-3.5 w-3.5" />
            </span>
          )}
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <h3 className="truncate text-sm font-medium text-foreground">{title}</h3>
              {badge && (
                <span className="shrink-0 rounded border border-border bg-elevated/70 px-1.5 py-0.5 text-[10px] text-secondary">
                  {badge}
                </span>
              )}
            </div>
            {description && <p className="mt-0.5 text-[11px] leading-4 text-muted">{description}</p>}
          </div>
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </header>
      <div className={cn(!flush && (unframed ? 'pt-4' : 'p-4'), contentClassName)}>{children}</div>
    </section>
  )
}

interface SettingsToggleRowProps {
  label: string
  description: string
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  icon?: SettingsIcon
  disabled?: boolean
  className?: string
}

export function SettingsToggleRow({
  label,
  description,
  checked,
  onCheckedChange,
  icon: Icon,
  disabled,
  className,
}: SettingsToggleRowProps) {
  const descriptionId = useId()

  return (
    <div className={cn('flex items-start justify-between gap-4 py-2.5', className)}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon && <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-secondary" />}
        <div className="min-w-0">
          <div className="text-sm text-foreground">{label}</div>
          <div id={descriptionId} className="mt-0.5 text-[11px] leading-4 text-muted">{description}</div>
        </div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        aria-describedby={descriptionId}
        disabled={disabled}
        onClick={() => onCheckedChange(!checked)}
        className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-btn focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:cursor-not-allowed disabled:opacity-40 sm:h-9"
      >
        <span className={cn('relative inline-flex h-5 w-9 items-center rounded-full transition-colors duration-200', checked ? 'bg-accent' : 'bg-elevated-2')}>
          <span className={cn('inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200', checked ? 'translate-x-[18px]' : 'translate-x-[3px]')} />
        </span>
      </button>
    </div>
  )
}

export const settingsControlClass =
  'h-11 w-full rounded-btn border border-border bg-base px-3 text-base text-foreground outline-none transition-colors placeholder:text-muted/60 focus-visible:border-accent/50 focus-visible:ring-2 focus-visible:ring-accent/15 disabled:cursor-not-allowed disabled:opacity-50 sm:h-9 sm:text-xs'

export const settingsPrimaryButtonClass =
  'inline-flex h-11 items-center justify-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-2 focus-visible:ring-offset-base disabled:cursor-not-allowed disabled:opacity-50 sm:h-9'

export const settingsSecondaryButtonClass =
  'inline-flex h-11 items-center justify-center gap-1.5 rounded-btn border border-border bg-base px-3 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50 sm:h-9'

export const settingsIconButtonClass =
  'inline-flex h-11 w-11 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:cursor-not-allowed disabled:opacity-50 sm:h-8 sm:w-8'
