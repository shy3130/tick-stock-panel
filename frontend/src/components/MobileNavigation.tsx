import { useEffect, useState, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Menu, X } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { Logo } from './Logo'
import { MarketFilterTabs } from './MarketFilterTabs'
import type { MarketCode } from '@/lib/market-display'

export function mobilePageTitle(
  pathname: string,
  items: ReadonlyArray<{ to: string; label: string }>,
): string {
  return [{ to: '/settings', label: '设置' }, ...items]
    .sort((a, b) => b.to.length - a.to.length)
    .find(item => (
      item.to === '/'
        ? pathname === '/'
        : pathname === item.to || pathname.startsWith(`${item.to}/`)
    ))
    ?.label ?? 'TickFlow'
}

export function MobileNavigation({
  title,
  market,
  onMarketChange,
  children,
}: {
  title: string
  market: MarketCode
  onMarketChange: (market: MarketCode) => void
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const location = useLocation()

  useEffect(() => setOpen(false), [location.pathname])

  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <>
      <header className="fixed inset-x-0 top-0 z-40 flex h-14 items-center gap-2 border-b border-border bg-surface/95 px-3 backdrop-blur md:hidden">
        <button
          type="button"
          aria-label="打开导航菜单"
          onClick={() => setOpen(true)}
          className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-btn bg-elevated text-secondary"
        >
          <Menu className="h-5 w-5" />
        </button>
        <Logo size={24} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</span>
        <MarketFilterTabs
          value={market}
          includeAll={false}
          className="h-8 shrink-0"
          onChange={next => { if (next !== 'all') onMarketChange(next) }}
        />
      </header>

      <AnimatePresence>
        {open && (
          <motion.button
            type="button"
            aria-label="关闭导航菜单"
            className="fixed inset-0 z-40 bg-black/65 md:hidden"
            onClick={() => setOpen(false)}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          />
        )}
      </AnimatePresence>

      <div
        role={open ? 'dialog' : undefined}
        aria-modal={open ? true : undefined}
        aria-label={open ? '主导航' : undefined}
        className={`fixed inset-y-0 left-0 z-50 h-full w-[min(19rem,86vw)] bg-surface shadow-2xl transition-transform duration-200 md:static md:z-auto md:block md:w-auto md:translate-x-0 md:visible md:shadow-none ${
          open ? 'visible translate-x-0' : 'invisible -translate-x-full'
        }`}
      >
        <button
          type="button"
          aria-label="关闭导航菜单"
          onClick={() => setOpen(false)}
          className="absolute right-2 top-2 z-10 inline-flex h-9 w-9 items-center justify-center rounded-btn bg-elevated text-secondary md:hidden"
        >
          <X className="h-4 w-4" />
        </button>
        {children}
      </div>
    </>
  )
}
