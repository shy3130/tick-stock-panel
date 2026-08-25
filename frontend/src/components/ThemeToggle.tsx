import { useCallback, useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { cn } from '@/lib/cn'

export type ThemeMode = 'dark' | 'light'

export const THEME_STORAGE_KEY = 'tickflow-theme'

function readStoredTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY)
    if (v === 'light' || v === 'dark') return v
  } catch {
    /* ignore */
  }
  return 'dark'
}

function applyTheme(theme: ThemeMode) {
  const root = document.documentElement
  if (theme === 'light') root.classList.remove('dark')
  else root.classList.add('dark')
  root.dataset.theme = theme
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    /* ignore */
  }
  // Keep browser chrome in sync
  const color = theme === 'light' ? '#f5f7fa' : '#0f1419'
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => {
    const media = m.getAttribute('media') || ''
    if (!media || media.includes(theme) || media.includes('prefers-color-scheme')) {
      m.setAttribute('content', color)
    }
  })
}

/** Compact light/dark toggle for context bar. Default dark; key = tickflow-theme. */
export function ThemeToggle({ className }: { className?: string }) {
  const [theme, setTheme] = useState<ThemeMode>(() => {
    if (typeof document === 'undefined') return 'dark'
    return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
  })

  useEffect(() => {
    // Reconcile with storage once mounted (covers multi-tab)
    const stored = readStoredTheme()
    setTheme(stored)
    applyTheme(stored)

    const onStorage = (e: StorageEvent) => {
      if (e.key !== THEME_STORAGE_KEY || !e.newValue) return
      if (e.newValue === 'light' || e.newValue === 'dark') {
        setTheme(e.newValue)
        applyTheme(e.newValue)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next: ThemeMode = prev === 'dark' ? 'light' : 'dark'
      applyTheme(next)
      return next
    })
  }, [])

  const isDark = theme === 'dark'

  return (
    <button
      type="button"
      onClick={toggle}
      className={cn(
        'btn-ghost h-8 w-8 px-0',
        className,
      )}
      title={isDark ? '切换浅色' : '切换深色'}
      aria-label={isDark ? '切换浅色主题' : '切换深色主题'}
      aria-pressed={!isDark}
    >
      {isDark ? (
        <Sun className="h-3.5 w-3.5" aria-hidden />
      ) : (
        <Moon className="h-3.5 w-3.5" aria-hidden />
      )}
    </button>
  )
}
