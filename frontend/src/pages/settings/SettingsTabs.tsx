import type { ComponentType } from 'react'

import { cn } from '@/lib/cn'

export type SettingsTabItem = {
  key: string
  label: string
  icon: ComponentType<{ className?: string }>
  badge?: string
}

type SettingsTabsProps = {
  tabs: readonly SettingsTabItem[]
  activeKey: string
  onChange: (key: string) => void
}

export function SettingsTabs({ tabs, activeKey, onChange }: SettingsTabsProps) {
  return (
    <nav aria-label="设置分类" className="w-full shrink-0 md:w-36">
      <div
        role="tablist"
        aria-orientation="horizontal"
        className="flex gap-1 overflow-x-auto pb-1 md:sticky md:top-6 md:min-h-[60vh] md:flex-col md:justify-center md:overflow-visible md:pb-0"
      >
        {tabs.map((tab) => {
          const Icon = tab.icon
          const selected = tab.key === activeKey

          return (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => onChange(tab.key)}
              className={cn(
                'relative flex min-w-max items-center gap-2 rounded-btn px-3 py-2 text-left text-sm transition-colors duration-150 ease-smooth md:w-full',
                selected
                  ? 'bg-accent/10 font-medium text-accent'
                  : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" />
              <span>{tab.label}</span>
              {tab.badge ? (
                <span className="ml-auto inline-flex shrink-0 items-center rounded-full border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-amber-400">
                  {tab.badge}
                </span>
              ) : null}
            </button>
          )
        })}
      </div>
    </nav>
  )
}
