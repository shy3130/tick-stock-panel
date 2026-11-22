import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { AlertCircle } from 'lucide-react'
import { cn } from '@/lib/cn'
import { errorMessage } from './viewModel'

export const inputClass =
  'min-h-8 w-full min-w-0 rounded-btn border border-border bg-base px-2.5 py-1 text-sm text-foreground tabular-nums focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 [@media(pointer:coarse)]:min-h-9'

export const workspaceClass = 'max-h-full min-h-0 min-w-0 overflow-y-auto rounded-card border border-border bg-surface lg:grid lg:grid-cols-[14rem_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)] lg:overflow-hidden'
export const workspaceSidebarClass = 'min-w-0 border-b border-border bg-base/35 lg:min-h-0 lg:overflow-y-auto lg:border-b-0 lg:border-r'
export const workspaceRowClass = 'w-full rounded-btn border border-transparent px-2 py-1.5 text-left transition-colors hover:bg-elevated/70 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent aria-[current=true]:border-border aria-[current=true]:bg-elevated/80'

const buttonVariants = {
  primary: 'border-transparent bg-blue-600 text-white hover:bg-blue-700 active:bg-blue-800 disabled:hover:bg-blue-600 disabled:active:bg-blue-600',
  secondary: 'border-border bg-surface text-foreground enabled:hover:border-accent/40 enabled:hover:bg-elevated',
  quiet: 'border-transparent text-secondary enabled:hover:bg-elevated enabled:hover:text-foreground',
  danger: 'border-danger/30 bg-danger/5 text-red-700 enabled:hover:bg-danger/10 dark:text-red-300',
}
type ButtonVariant = keyof typeof buttonVariants

export function buttonClass(variant: ButtonVariant = 'secondary', compact = false) {
  return cn(
    'inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-btn border py-1 font-medium leading-4 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none [@media(pointer:coarse)]:min-h-9 [&_svg]:shrink-0',
    compact ? 'min-h-7 px-2 text-xs' : 'min-h-8 px-2.5 text-[13px]',
    buttonVariants[variant],
  )
}

export function Button({
  variant,
  compact,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; compact?: boolean }) {
  return (
    <button
      type="button"
      {...props}
      className={cn(buttonClass(variant, compact), className)}
    />
  )
}

export function ErrorNotice({ error, children }: { error: unknown; children?: ReactNode }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-start gap-2 rounded-btn border border-danger/30 bg-danger/5 p-2.5 text-sm"
    >
      <AlertCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
      <div className="min-w-0 flex-1 space-y-2 break-words">
        <p>{typeof error === 'string' ? error : errorMessage(error)}</p>
        {children}
      </div>
    </div>
  )
}

export function LoadingLines({ label = '正在加载' }: { label?: string }) {
  return (
    <div role="status" aria-label={label} className="space-y-2 p-3">
      <span className="text-sm text-secondary">{label}…</span>
      <div aria-hidden="true" className="h-8 rounded bg-elevated" />
      <div aria-hidden="true" className="h-10 rounded bg-elevated" />
    </div>
  )
}
