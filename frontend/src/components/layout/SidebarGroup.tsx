import type { CSSProperties, ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useGroupOpen } from '@/lib/sidebarState'
import { NAV_GROUP_COLOR, NAV_GROUP_LABEL, type NavGroup, type NavItem } from '@/lib/navRegistry'

interface Props {
  group: NavGroup
  items: NavItem[]
  collapsed: boolean
  renderBadge: (item: NavItem, isActive: boolean) => ReactNode
}

/**
 * 侧栏单个分组 —— 展开态: 可点击的分组标题(记忆展开/收起状态) + 导航项列表,
 * 每项用图标 chip + 左侧色条标出当前分组色(NAV_GROUP_COLOR),替代原来统一的
 * accent 高亮,让四个分组在视觉上可区分。
 * 折叠态(图标条): 跳过分组标题,只保留图标 chip,触摸区固定 44px 高(WCAG 最小
 * 触控目标),组间留一点空隙做视觉分隔。
 */
export function SidebarGroup({ group, items, collapsed, renderBadge }: Props) {
  const [open, toggle] = useGroupOpen(group)
  const groupColor = NAV_GROUP_COLOR[group]

  if (items.length === 0) return null

  if (collapsed) {
    return (
      <div className="py-1">
        {items.map(({ to, label, icon: Icon }) => (
          <NavLink key={to} to={to} title={label} aria-label={label} className="flex h-11 items-center justify-center">
            {({ isActive }) => (
              <span
                className={cn(
                  'flex h-8 w-8 items-center justify-center rounded-md transition-colors duration-150 ease-smooth',
                  isActive ? 'text-[var(--g)]' : 'text-foreground/70 hover:text-foreground',
                )}
                style={{
                  '--g': groupColor,
                  backgroundColor: isActive ? 'color-mix(in srgb, var(--g) 20%, transparent)' : undefined,
                } as CSSProperties}
              >
                <Icon className="h-4 w-4 shrink-0" />
              </span>
            )}
          </NavLink>
        ))}
      </div>
    )
  }

  return (
    <div className="pb-1">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted hover:text-secondary transition-colors"
      >
        <ChevronDown className={cn('h-3 w-3 shrink-0 transition-transform duration-150 ease-smooth', !open && '-rotate-90')} />
        <span className="flex-1 text-left">{NAV_GROUP_LABEL[group]}</span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="sidebar-group-items"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0, transition: { duration: 0.15, ease: 'easeOut' } }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            style={{ overflow: 'hidden' }}
          >
            <div className="space-y-0.5" style={{ '--g': groupColor } as CSSProperties}>
              {items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      'group flex items-center gap-2.5 rounded-btn py-1.5 pl-[10px] pr-3 text-sm border-l-2 transition-colors duration-150 ease-smooth',
                      isActive
                        ? 'border-[var(--g)] bg-elevated/60 text-foreground font-medium'
                        : 'border-transparent text-foreground/80 hover:bg-elevated hover:text-foreground',
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <span
                        className={cn(
                          'flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors duration-150 ease-smooth',
                          isActive ? 'text-[var(--g)]' : 'bg-elevated text-foreground/70 group-hover:text-foreground',
                        )}
                        style={{ backgroundColor: isActive ? 'color-mix(in srgb, var(--g) 15%, transparent)' : undefined }}
                      >
                        <item.icon className="h-4 w-4" />
                      </span>
                      <span className="flex-1">{item.label}</span>
                      {renderBadge(item, isActive)}
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
