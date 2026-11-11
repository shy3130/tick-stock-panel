import { useLayoutEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown, ChevronRight, Folder, FolderOpen } from 'lucide-react'
import { cn } from '@/lib/cn'

export function NavFolder({ label, active, expanded, rail, popupOpen, onToggle, onPopupChange, children, indicators }: {
  label: string; active: boolean; expanded: boolean; rail: boolean; popupOpen: boolean
  onToggle: () => void; onPopupChange: (open: boolean) => void
  children: ReactNode; indicators?: ReactNode
}) {
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const popupChange = useRef(onPopupChange)
  popupChange.current = onPopupChange
  const [position, setPosition] = useState({ left: 64, top: 8, maxHeight: 320 })
  useLayoutEffect(() => {
    if (!rail || !popupOpen) return
    const rect = trigger.current?.getBoundingClientRect()
    if (rect) {
      const top = Math.max(8, Math.min(rect.top, window.innerHeight - 320))
      setPosition({ left: Math.max(8, Math.min(rect.right + 8, window.innerWidth - 248)),
        top, maxHeight: Math.max(80, window.innerHeight - top - 8) })
    }
    const focus = window.requestAnimationFrame(() => (panel.current?.querySelector<HTMLElement>('a,button') ?? panel.current)?.focus())
    const outside = (event: MouseEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) popupChange.current(false)
    }
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { popupChange.current(false); trigger.current?.focus() }
    }
    const close = () => popupChange.current(false)
    const scroll = (event: Event) => {
      if (!panel.current?.contains(event.target as Node)) close()
    }
    document.addEventListener('mousedown', outside)
    document.addEventListener('keydown', key)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', scroll, true)
    return () => {
      window.cancelAnimationFrame(focus)
      document.removeEventListener('mousedown', outside)
      document.removeEventListener('keydown', key)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', scroll, true)
    }
  }, [rail, popupOpen])
  const open = rail ? popupOpen : expanded
  const Icon = open ? FolderOpen : Folder
  return <div>
    <button ref={trigger} type="button" aria-label={label} aria-expanded={open} aria-controls={id}
      aria-haspopup={rail ? 'dialog' : undefined}
      title={rail ? label : undefined} onClick={() => rail ? onPopupChange(!popupOpen) : onToggle()}
      className={cn('group flex w-full items-center gap-3 rounded-btn py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50',
        rail ? 'justify-center px-0' : 'px-3', active ? 'bg-elevated text-accent font-medium' : 'text-foreground/75 hover:bg-elevated/70 hover:text-foreground')}>
      <Icon className="h-4 w-4 shrink-0" />
      {!rail && <><span className="min-w-0 flex-1 truncate text-left">{label}</span>{indicators}
        {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />}</>}
    </button>
    {!rail && expanded && <div id={id} className="relative mt-0.5 space-y-0.5 pl-6">
      <span aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-3 border-l border-border/60" />{children}
    </div>}
    {rail && popupOpen && createPortal(<div ref={panel} id={id} role="dialog" aria-label={`${label}子菜单`} tabIndex={-1}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) onPopupChange(false) }}
      style={position} className="fixed z-[100] w-60 max-h-[min(70vh,32rem)] overflow-y-auto rounded-xl border border-border bg-surface p-2 shadow-xl">
      <div className="px-3 py-2 text-xs font-medium text-muted">{label}</div>{children}
    </div>, document.body)}
  </div>
}
