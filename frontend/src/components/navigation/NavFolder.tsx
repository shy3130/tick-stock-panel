import { useLayoutEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ChevronRight, Folder, FolderOpen } from 'lucide-react'

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
  return (
    <div>
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-haspopup={rail ? 'dialog' : undefined}
        title={label}
        onClick={() => rail ? onPopupChange(!popupOpen) : onToggle()}
        data-active={active}
        data-rail={rail}
        className="tsp-nav-row"
      >
        <span className="tsp-nav-icon" aria-hidden="true"><Icon className="h-4 w-4" /></span>
        {!rail && (
          <>
            <span className="tsp-nav-label">{label}</span>
            {indicators}
            <ChevronRight className="tsp-nav-chevron" aria-hidden="true" />
          </>
        )}
      </button>
      {!rail && expanded && <div id={id} className="tsp-nav-children">{children}</div>}
      {rail && popupOpen && createPortal(
        <div
          ref={panel}
          id={id}
          role="dialog"
          aria-label={`${label}子菜单`}
          tabIndex={-1}
          onBlur={event => {
            const next = event.relatedTarget as Node | null
            if (next !== trigger.current && !event.currentTarget.contains(next)) onPopupChange(false)
          }}
          style={position}
          className="tsp-nav-flyout tsp-nav-folder-panel"
        >
          <div className="tsp-nav-folder-label">{label}</div>
          {children}
        </div>, document.body,
      )}
    </div>
  )
}
