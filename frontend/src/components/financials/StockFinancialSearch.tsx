import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { Search, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { boardTag } from '@/components/stock-table/primitives'

interface Props {
  onSelect: (symbol: string, name: string) => void
  /** 搜索资产类型, 逗号分隔 (默认 'stock')。如 'stock,index' */
  assetTypes?: string
}

/**
 * 个股模糊搜索框 —— 财务页主入口。
 * 复用 instrumentSearch 后端(代码 / 名称模糊匹配),单选即跳转该股财务详情。
 * 模式对齐 Watchlist.StockSearchBox:useQuery + 外部点击关闭 + 键盘导航。
 */
export function StockFinancialSearch({ onSelect, assetTypes }: Props) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const search = useQuery({
    queryKey: QK.instrumentSearch(query, assetTypes ?? 'stock'),
    queryFn: () => api.instrumentSearch(query, 20, assetTypes),
    enabled: query.trim().length > 0,
    staleTime: 30_000,
  })

  const results = search.data?.results ?? []

  // 外部点击关闭下拉
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function handleSelect(r: { symbol: string; name: string }) {
    onSelect(r.symbol, r.name)
    setQuery('')
    setOpen(false)
    setActiveIdx(-1)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || results.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIdx >= 0) handleSelect(results[activeIdx])
      else if (results.length > 0) handleSelect(results[0])
    }
  }

  const trimmed = query.trim()

  return (
    <div ref={containerRef} className="relative w-full max-w-xl mx-auto">
      <div className="relative flex items-center">
        <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        <input
          ref={inputRef}
          type="text"
          placeholder="输入股票代码或名称，如 600000 / 浦发"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); setActiveIdx(-1) }}
          onFocus={() => { if (trimmed) setOpen(true) }}
          onKeyDown={handleKeyDown}
          // 较宽、更醒目 —— 作为财务页主入口
          className="w-full h-11 pl-11 pr-10 rounded-card bg-surface border border-border text-sm text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 focus:bg-base transition-colors"
        />
        {search.isFetching && (
          <Loader2 className="absolute right-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted animate-spin" />
        )}
      </div>

      <AnimatePresence>
        {open && trimmed && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className="absolute left-0 right-0 top-full mt-1.5 z-50 max-h-[360px] overflow-y-auto rounded-card border border-border bg-base shadow-xl"
          >
            {search.isLoading ? (
              <div className="px-4 py-6 flex items-center justify-center gap-2 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                搜索中…
              </div>
            ) : results.length === 0 ? (
              <div className="px-4 py-6 text-center text-xs text-muted">
                未找到匹配的股票
              </div>
            ) : (
              results.map((r, i) => (
                <button
                  key={r.symbol}
                  type="button"
                  onClick={() => handleSelect(r)}
                  className={`w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors duration-100 ${
                    i === activeIdx ? 'bg-accent/10 text-accent' : 'hover:bg-elevated text-foreground'
                  }`}
                >
                  <span className="font-mono shrink-0 text-xs w-[88px]">{r.symbol}</span>
                  <span className="truncate text-sm flex-1">{r.name}</span>
                  {(() => {
                    const b = boardTag(r.symbol)
                    return b && (
                      <span className={`shrink-0 px-1 py-0.5 rounded text-[10px] leading-none border ${b.color}`}>{b.label}</span>
                    )
                  })()}
                  {r.asset_type === 'index' && (
                    <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-sky-500/10 text-sky-400">指数</span>
                  )}
                  {r.code && <span className="text-[10px] text-muted font-mono shrink-0">{r.code}</span>}
                </button>
              ))
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
