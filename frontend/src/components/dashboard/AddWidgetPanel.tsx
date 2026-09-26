/**
 * 看板「添加组件」面板 — 内置组件列表 + 外部链接表单。
 *
 * 内置组件单实例: 已在布局中的禁用; 外部链接可加多个实例(URL 经消毒)。
 * 弹层用 fixed 定位锚定按钮下方, 点击面板外/Esc 关闭。
 */
import { useEffect, useRef, useState } from 'react'
import { Plus } from 'lucide-react'
import { cn } from '@/lib/cn'
import { sanitizeExtUrl, type WidgetType } from './layout'
import { WIDGET_DEFS } from './registry'

export function AddWidgetPanel({
  placedTypes,
  onAdd,
}: {
  placedTypes: Set<WidgetType>
  onAdd: (t: WidgetType, props?: Record<string, string>) => void
}) {
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState('')
  const [url, setUrl] = useState('')
  const [urlError, setUrlError] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDocMouseDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const addExtLink = () => {
    const clean = sanitizeExtUrl(url)
    if (!clean) {
      setUrlError('仅支持 http(s) 链接, 且不能是本站地址')
      return
    }
    onAdd('ext-link', { title: title.trim() || new URL(clean).hostname, url: clean })
    setTitle('')
    setUrl('')
    setUrlError('')
    setOpen(false)
  }

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className={cn(
          'inline-flex items-center gap-1 rounded-btn border px-2 py-1 text-[11px] transition-colors',
          open
            ? 'border-accent/50 bg-accent/10 text-accent'
            : 'border-border bg-elevated text-secondary hover:text-foreground hover:border-accent/40',
        )}
      >
        <Plus className="h-3 w-3" />添加组件
      </button>

      {open && (
        <div className="absolute left-0 top-7 z-50 w-72 rounded-card border border-border bg-surface p-2 shadow-2xl shadow-black/40">
          <div className="mb-1.5 px-0.5 text-[10px] font-medium text-muted">内置组件</div>
          <div className="max-h-56 space-y-0.5 overflow-y-auto">
            {WIDGET_DEFS.filter(d => d.id !== 'ext-link').map(d => {
              const placed = placedTypes.has(d.id)
              return (
                <button
                  key={d.id}
                  type="button"
                  disabled={placed}
                  onClick={() => { onAdd(d.id); setOpen(false) }}
                  className={cn(
                    'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors',
                    placed
                      ? 'cursor-default text-muted/50'
                      : 'cursor-pointer text-secondary hover:bg-elevated hover:text-foreground',
                  )}
                  title={placed ? '已在布局中' : undefined}
                >
                  <d.icon className="h-3.5 w-3.5 shrink-0 text-accent" />
                  <span className="flex-1 truncate">{d.label}</span>
                  {placed && <span className="text-[9px]">已添加</span>}
                </button>
              )
            })}
          </div>

          <div className="mt-2 border-t border-border/60 pt-2">
            <div className="mb-1.5 px-0.5 text-[10px] font-medium text-muted">外部链接(可加多个)</div>
            <div className="space-y-1.5">
              <input
                value={title}
                onChange={e => setTitle(e.target.value)}
                placeholder="标题, 如: 东财行情"
                maxLength={30}
                className="w-full rounded-btn bg-base px-2 py-1.5 text-xs text-foreground ring-1 ring-border/40 placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/40"
              />
              <input
                value={url}
                onChange={e => { setUrl(e.target.value); setUrlError('') }}
                placeholder="https://… (仅 http/https)"
                className="w-full rounded-btn bg-base px-2 py-1.5 font-mono text-xs text-foreground ring-1 ring-border/40 placeholder:text-muted/40 focus:outline-none focus:ring-2 focus:ring-accent/40"
              />
              {urlError && <div className="text-[10px] text-danger">{urlError}</div>}
              <button
                type="button"
                onClick={addExtLink}
                className="w-full rounded-btn bg-accent/15 px-2 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/25"
              >
                添加到看板
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
