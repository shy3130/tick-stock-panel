/**
 * 自定义侧栏导航图标 —— 语义化重绘的 10 个高频入口图标。风格严格对齐 Lucide 的
 * 技术参数(stroke-width 2 / round linecap+linejoin / no fill / viewBox 0 0 24 24),
 * 保证跟其余仍用 lucide-react 原图标的分组混排时不违和。
 * 未在这里重绘的图标(概念分析/行业分析/财务分析/复盘)继续用 navRegistry.ts 里的
 * lucide-react 原图标,不在本次改动范围内。
 */
type IconProps = { className?: string }

export function DashboardIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <line x1="5" y1="3" x2="5" y2="21" />
      <rect x="3" y="9" width="4" height="6" rx="0.5" />
      <line x1="12" y1="2" x2="12" y2="21" />
      <rect x="10" y="6" width="4" height="9" rx="0.5" />
      <line x1="19" y1="7" x2="19" y2="22" />
      <rect x="17" y="12" width="4" height="6" rx="0.5" />
    </svg>
  )
}

export function WatchlistIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M6 3h12a1 1 0 011 1v17l-7-4-7 4V4a1 1 0 011-1z" />
    </svg>
  )
}

export function IndicesIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M2 8c2-4 4-4 6 0s4 4 6 0 4-4 6 0" opacity={0.4} />
      <path d="M2 17c2-4 4-4 6 0s4 4 6 0 4-4 6 0" />
    </svg>
  )
}

export function StrategyIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M3 4h18l-7 8.5V19l-4 2v-8.5z" />
      <path d="M16 16.3l1.6 1.6 3-3.6" />
    </svg>
  )
}

export function BacktestIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M3 12a9 9 0 109-9 9.75 9.75 0 00-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
      <path d="M9.5 13.2l1.8-2.7 1.8 1.8 2.7-3.6" />
    </svg>
  )
}

export function LimitLadderIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M3 20h4v-4h4v-4h4v-4h4V4" />
    </svg>
  )
}

export function StockAnalysisIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <circle cx="10" cy="10" r="7" />
      <path d="M20 20l-4.3-4.3" />
      <path d="M6.8 11.3l1.8-2.4 1.7 1.4 2.5-3.3" />
    </svg>
  )
}

export function MonitorIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M18 8a6 6 0 00-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.7 21a2 2 0 01-3.4 0" />
      <path d="M8.5 8l1.6 2.2 1.6-3.2 1.6 2.6 1.6-1.6" />
    </svg>
  )
}

export function DataIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <ellipse cx="10" cy="5" rx="7" ry="2.3" />
      <path d="M3 5v12c0 1.3 3.1 2.3 7 2.3.9 0 1.8-.06 2.6-.16" />
      <path d="M3 11c0 1.3 3.1 2.3 7 2.3" />
      <path d="M17 14.8a2.8 2.8 0 004.9 1.9M22.2 14.2l.3 2.7-2.7-.3" />
    </svg>
  )
}

export function TradingIcon({ className }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d="M7 3L3 7l4 4" />
      <path d="M3 7h12a4 4 0 014 4v1" />
      <path d="M17 21l4-4-4-4" />
      <path d="M21 17H9a4 4 0 01-4-4v-1" />
    </svg>
  )
}
