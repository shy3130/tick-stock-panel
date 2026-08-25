/**
 * Shared visual primitives for Quant Research Workbench.
 *
 * All class strings are static (safe for Tailwind purge). Compose with cn().
 * Prefer Foundation classes: panel / panel-header / panel-body / control /
 * btn-primary / btn-secondary / btn-ghost / data-table / metric-value / status-dot.
 *
 * A-share bull/bear tokens are reserved for price/quote semantics only.
 */

import type { ButtonHTMLAttributes, HTMLAttributes, InputHTMLAttributes, SelectHTMLAttributes } from 'react'
import { cn } from '@/lib/cn'

// ── Static semantic maps ──────────────────────────────────────────

/** Status / severity surface tokens (border + bg + text). */
export const STATUS_TONE = {
  neutral: 'border-border bg-elevated/40 text-muted',
  info: 'border-accent/30 bg-accent/5 text-accent',
  success: 'border-success/30 bg-success/5 text-success',
  warning: 'border-warning/30 bg-warning/5 text-warning',
  danger: 'border-danger/30 bg-danger/5 text-danger',
  /** External / degraded path — warning semantics, not brand amber. */
  degraded: 'border-warning/35 bg-warning/10 text-warning',
} as const

export type StatusTone = keyof typeof STATUS_TONE

/** Compact pill / badge fills. */
export const BADGE_TONE = {
  neutral: 'border-border bg-elevated text-muted',
  accent: 'border-accent/30 bg-accent/10 text-accent',
  success: 'border-success/30 bg-success/10 text-success',
  warning: 'border-warning/30 bg-warning/10 text-warning',
  danger: 'border-danger/30 bg-danger/10 text-danger',
  info: 'border-info/30 bg-info/10 text-info',
  muted: 'border-border bg-base/40 text-secondary',
} as const

export type BadgeTone = keyof typeof BADGE_TONE

/** Solid fill for dots / bars (no dynamic template strings). */
export const SOLID_TONE = {
  muted: 'bg-muted',
  accent: 'bg-accent',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-info',
  bull: 'bg-bull',
  bear: 'bg-bear',
} as const

export type SolidTone = keyof typeof SOLID_TONE

export const TEXT_TONE = {
  muted: 'text-muted',
  secondary: 'text-secondary',
  foreground: 'text-foreground',
  accent: 'text-accent',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  info: 'text-info',
  bull: 'text-bull',
  bear: 'text-bear',
} as const

export type TextTone = keyof typeof TEXT_TONE

/** A-share price direction — only for quote/price display. */
export const PRICE_DIR = {
  bull: { text: 'text-bull', bg: 'bg-bull', soft: 'bg-bull/5', border: 'border-bull/30' },
  bear: { text: 'text-bear', bg: 'bg-bear', soft: 'bg-bear/5', border: 'border-bear/30' },
  flat: { text: 'text-muted', bg: 'bg-muted', soft: 'bg-elevated', border: 'border-border' },
} as const

export type PriceDir = keyof typeof PRICE_DIR

// ── Helpers ───────────────────────────────────────────────────────

export function statusTone(tone: StatusTone | string | undefined, fallback: StatusTone = 'neutral'): string {
  if (tone && tone in STATUS_TONE) return STATUS_TONE[tone as StatusTone]
  return STATUS_TONE[fallback]
}

export function badgeTone(tone: BadgeTone | string | undefined, fallback: BadgeTone = 'neutral'): string {
  if (tone && tone in BADGE_TONE) return BADGE_TONE[tone as BadgeTone]
  return BADGE_TONE[fallback]
}

export function solidTone(tone: SolidTone | string | undefined, fallback: SolidTone = 'muted'): string {
  if (tone && tone in SOLID_TONE) return SOLID_TONE[tone as SolidTone]
  return SOLID_TONE[fallback]
}

export function textTone(tone: TextTone | string | undefined, fallback: TextTone = 'muted'): string {
  if (tone && tone in TEXT_TONE) return TEXT_TONE[tone as TextTone]
  return TEXT_TONE[fallback]
}

export function priceDir(dir: 'bull' | 'bear' | 'flat' | number | null | undefined): (typeof PRICE_DIR)[PriceDir] {
  if (dir === 'bull' || dir === 'bear' || dir === 'flat') return PRICE_DIR[dir]
  if (typeof dir === 'number') {
    if (dir > 0) return PRICE_DIR.bull
    if (dir < 0) return PRICE_DIR.bear
  }
  return PRICE_DIR.flat
}

// ── Leaf components ───────────────────────────────────────────────

export function Panel({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('panel', className)} {...props} />
}

export function PanelHeader({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('panel-header', className)} {...props} />
}

export function PanelBody({
  className,
  ...props
}: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('panel-body', className)} {...props} />
}

type BtnVariant = 'primary' | 'secondary' | 'ghost'

const BTN_VARIANT: Record<BtnVariant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
}

export function Btn({
  variant = 'secondary',
  className,
  type = 'button',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant }) {
  return (
    <button
      type={type}
      className={cn(BTN_VARIANT[variant], className)}
      {...props}
    />
  )
}

export function Control({
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn('control', className)} {...props} />
}

export function ControlSelect({
  className,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn('control', className)} {...props} />
}

export function StatusDot({
  state = 'idle',
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & {
  state?: 'live' | 'ready' | 'ok' | 'warn' | 'degraded' | 'error' | 'danger' | 'idle' | 'off'
}) {
  return (
    <span
      className={cn('status-dot', className)}
      data-state={state}
      aria-hidden
      {...props}
    />
  )
}

export function MetricValue({
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn('metric-value', className)} {...props} />
}

/** Compact semantic badge / chip. */
export function Badge({
  tone = 'neutral',
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded border px-1.5 py-px text-[10px] font-medium leading-tight',
        BADGE_TONE[tone],
        className,
      )}
      {...props}
    />
  )
}

/** Toast / alert solid surfaces (opaque enough for both themes). */
export const TOAST_KIND = {
  error: 'border border-danger/40 bg-danger text-white',
  success: 'border border-success/40 bg-success text-white',
  warning: 'border border-warning/40 bg-warning text-white',
  info: 'border border-accent/40 bg-accent text-white',
} as const

export type ToastKind = keyof typeof TOAST_KIND
