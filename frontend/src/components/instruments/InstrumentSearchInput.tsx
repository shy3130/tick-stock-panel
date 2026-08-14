import { createPortal } from 'react-dom'
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Search } from 'lucide-react'
import {
  api,
  type InstrumentAssetType,
  type InstrumentSearchResult,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { instrumentSearchMeta } from '@/lib/instrumentSearch'
import { QK } from '@/lib/queryKeys'

interface InstrumentSearchInputProps {
  value: string
  onChange: (value: string) => void
  onSelect?: (result: InstrumentSearchResult) => void
  assetTypes?: readonly InstrumentAssetType[]
  placeholder?: string
  ariaLabel?: string
  className?: string
  inputClassName?: string
  dropdownClassName?: string
  limit?: number
  disabled?: boolean
  clearOnSelect?: boolean
  portal?: boolean
}

/**
 * 标准标的检索入口：代码、名称、全拼与简拼均通过 instrument search API 解析。
 *
 * 新的单标的交互必须复用本组件并按业务能力传入 `assetTypes`；调用方仍持有输入
 * 和显式提交语义，选中结果只写 canonical `result.symbol`。逗号分隔的批量字段应
 * 保留原文本和“留空=全市场”语义，并使用 `InstrumentSearchAdder` 追加去重标的。
 */
export function InstrumentSearchInput({
  value,
  onChange,
  onSelect,
  assetTypes,
  placeholder = '输入代码、名称或拼音',
  ariaLabel = '标的搜索',
  className,
  inputClassName = 'control w-full text-xs',
  dropdownClassName,
  limit = 20,
  disabled = false,
  clearOnSelect = false,
  portal = false,
}: InstrumentSearchInputProps) {
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [menuPosition, setMenuPosition] = useState({
    top: 0,
    left: 0,
    width: 0,
    maxHeight: 288,
  })
  const containerRef = useRef<HTMLDivElement>(null)
  const listboxRef = useRef<HTMLDivElement>(null)
  const listId = useId()
  const query = value.trim()

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, assetTypes, limit),
    queryFn: () => api.instrumentSearch(query, limit, assetTypes),
    enabled: open && query.length > 0,
    staleTime: 30_000,
  })
  const results = search.data?.results ?? []

  useEffect(() => {
    const closeOnOutsidePointer = (event: MouseEvent) => {
      const target = event.target as Node
      if (
        containerRef.current &&
        !containerRef.current.contains(target) &&
        !listboxRef.current?.contains(target)
      ) {
        setOpen(false)
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', closeOnOutsidePointer)
    return () => document.removeEventListener('mousedown', closeOnOutsidePointer)
  }, [])

  useEffect(() => {
    if (!portal || !open) return
    const updateMenuPosition = () => {
      const input = containerRef.current?.querySelector('input')
      if (!input) return
      const rect = input.getBoundingClientRect()
      const viewportWidth = window.innerWidth
      const viewportHeight = window.innerHeight
      const availableWidth = Math.max(viewportWidth - 16, 0)
      const width = Math.min(Math.max(rect.width, 288), availableWidth)
      const left = Math.min(
        Math.max(rect.left, 8),
        Math.max(viewportWidth - width - 8, 8),
      )
      const gap = 4
      const viewportMargin = 8
      const availableBelow = Math.max(viewportHeight - rect.bottom - gap - viewportMargin, 0)
      const availableAbove = Math.max(rect.top - gap - viewportMargin, 0)
      const placeBelow = availableBelow >= availableAbove || availableBelow >= 288
      const availableHeight = Math.min(
        288,
        placeBelow ? availableBelow : availableAbove,
      )
      const top = placeBelow
        ? rect.bottom + gap
        : rect.top - gap - availableHeight
      setMenuPosition({ top, left, width, maxHeight: availableHeight })
    }
    updateMenuPosition()
    window.addEventListener('resize', updateMenuPosition)
    document.addEventListener('scroll', updateMenuPosition, true)
    return () => {
      window.removeEventListener('resize', updateMenuPosition)
      document.removeEventListener('scroll', updateMenuPosition, true)
    }
  }, [open, portal])

  const selectResult = (result: InstrumentSearchResult) => {
    onChange(clearOnSelect ? '' : result.symbol)
    onSelect?.(result)
    setOpen(false)
    setActiveIndex(-1)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Escape') {
      setOpen(false)
      setActiveIndex(-1)
      return
    }
    if (!open || results.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((index) => Math.min(index + 1, results.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((index) => Math.max(index - 1, -1))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      selectResult(results[activeIndex >= 0 ? activeIndex : 0])
    }
  }

  const listbox = (
    <div
      ref={listboxRef}
      id={listId}
      role="listbox"
      aria-label={`${ariaLabel}结果`}
      className={cn(
        portal
          ? 'fixed z-50 max-h-72 overflow-y-auto rounded-card border border-border bg-base shadow-xl'
          : 'absolute left-0 right-0 top-full z-50 mt-1 max-h-72 overflow-y-auto rounded-card border border-border bg-base shadow-xl',
        dropdownClassName,
      )}
      style={portal ? menuPosition : undefined}
    >
      {search.isLoading ? (
        <div className="flex items-center justify-center gap-2 px-3 py-4 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />搜索中…
        </div>
      ) : search.isError ? (
        <div className="px-3 py-4 text-center text-xs text-danger">标的搜索失败，请稍后重试。</div>
      ) : results.length === 0 ? (
        <div className="px-3 py-4 text-center text-xs text-muted">未找到匹配标的</div>
      ) : results.map((result, index) => {
        const meta = instrumentSearchMeta(result)
        return (
          <button
            id={`${listId}-${index}`}
            key={result.symbol}
            type="button"
            role="option"
            aria-selected={index === activeIndex}
            onClick={() => selectResult(result)}
            className={cn(
              'flex w-full items-center gap-2 px-3 py-2 text-left transition-colors',
              index === activeIndex ? 'bg-accent/10 text-accent' : 'text-foreground hover:bg-elevated',
            )}
          >
            <span className="w-[88px] shrink-0 font-mono text-xs">{result.symbol}</span>
            <span className="min-w-0 flex-1 truncate text-xs">{result.name}</span>
            {meta && <span className="shrink-0 text-[10px] text-muted">{meta}</span>}
          </button>
        )
      })}
    </div>
  )

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      <Search className="pointer-events-none absolute left-3 top-1/2 z-10 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
      <input
        type="text"
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
          setOpen(true)
          setActiveIndex(-1)
        }}
        onFocus={() => {
          if (query) setOpen(true)
        }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-autocomplete="list"
        aria-controls={open && query ? listId : undefined}
        aria-expanded={open && query}
        aria-activedescendant={activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
        autoCapitalize="characters"
        disabled={disabled}
        className={cn(inputClassName, 'pl-8')}
      />
      {search.isFetching && <Loader2 className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-muted" />}
      {open && query && (portal ? createPortal(listbox, document.body) : listbox)}
    </div>
  )
}

interface InstrumentSearchAdderProps {
  onAdd: (result: InstrumentSearchResult) => void
  assetTypes?: readonly InstrumentAssetType[]
  placeholder?: string
  ariaLabel?: string
  className?: string
  inputClassName?: string
}

/** 为逗号分隔的批量标的字段提供名称/拼音添加入口，不改变原字段语义。 */
export function InstrumentSearchAdder({
  onAdd,
  assetTypes,
  placeholder = '搜索代码、名称或拼音后添加',
  ariaLabel = '添加标的',
  className,
  inputClassName,
}: InstrumentSearchAdderProps) {
  const [query, setQuery] = useState('')
  return (
    <InstrumentSearchInput
      value={query}
      onChange={setQuery}
      onSelect={onAdd}
      assetTypes={assetTypes}
      placeholder={placeholder}
      ariaLabel={ariaLabel}
      className={className}
      inputClassName={inputClassName}
      clearOnSelect
    />
  )
}
