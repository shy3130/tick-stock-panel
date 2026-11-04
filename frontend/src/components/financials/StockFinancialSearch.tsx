import { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Search, Loader2 } from 'lucide-react'
import { useInstrumentSearch, ASSET_TYPE_LABELS, type InstrumentSearchRow } from '@/lib/useInstrumentSearch'
import { AssetTypeFilter } from '@/components/AssetTypeFilter'
import { boardTag } from '@/components/stock-table/primitives'

interface Props {
  onSelect: (symbol: string, name: string) => void
  /** 搜索资产类型, 逗号分隔 (默认 'stock')。如 'stock,index'
   *  透传给 api.instrumentSearch(q, limit, assetTypes); 同时决定下拉内筛选档位 */
  assetTypes?: string
}

/**
 * 个股模糊搜索框 —— 财务 / 缠论 / 个股分析页共用。
 * 单选即跳转该股详情; 数据层由 useInstrumentSearch 提供
 * (输入防抖 + 后端游标分页 + 下拉内资产类型筛选)。
 * 追加下一页时不改动 scrollTop —— 已有结果原地不动, 新结果接在后面。
 */
export function StockFinancialSearch({ onSelect, assetTypes }: Props) {
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const s = useInstrumentSearch(assetTypes)

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

  function handleSelect(r: InstrumentSearchRow) {
    onSelect(r.symbol, r.name)
    s.clearQuery()
    setOpen(false)
    setActiveIdx(-1)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { setOpen(false); return }
    if (!open || s.results.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => Math.min(i + 1, s.results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => Math.max(i - 1, -1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIdx >= 0 && activeIdx < s.results.length) handleSelect(s.results[activeIdx])
      else if (s.results.length > 0) handleSelect(s.results[0])
    }
  }

  return (
    <div ref={containerRef} className="relative w-full max-w-xl mx-auto">
      <div className="relative flex items-center">
        <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
        <input
          ref={inputRef}
          type="text"
          placeholder="输入股票代码或名称，如 600000 / 浦发"
          value={s.query}
          onChange={(e) => { s.setQuery(e.target.value); setOpen(true); setActiveIdx(-1) }}
          onFocus={() => { if (s.trimmed) setOpen(true) }}
          onKeyDown={handleKeyDown}
          // 较宽、更醒目 —— 作为财务页主入口
          className="w-full h-11 pl-11 pr-10 rounded-card bg-surface border border-border text-sm text-foreground placeholder:text-muted focus:outline-none focus:border-accent/50 focus:bg-base transition-colors"
        />
        {s.isFetching && (
          <Loader2 className="absolute right-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted animate-spin" />
        )}
      </div>

      <AnimatePresence>
        {/* 防抖期间 trimmed 还是旧值/空, 用 results 兜底保持下拉连续 (与自选页一致) */}
        {open && (s.trimmed || s.results.length > 0) && (
          <motion.div
            ref={s.listRef}
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className="absolute left-0 right-0 top-full mt-1.5 z-50 max-h-[360px] overflow-y-auto rounded-card border border-border bg-base shadow-xl"
          >
            {s.showFilter && (
              <div className="sticky top-0 z-10 px-2 py-1.5 border-b border-border/60 bg-base/95 backdrop-blur">
                <AssetTypeFilter
                  types={s.types}
                  value={s.filter}
                  onChange={(next) => { s.setFilter(next); setActiveIdx(-1) }}
                  count={s.results.length}
                />
              </div>
            )}

            {s.isLoading ? (
              <div className="px-4 py-6 flex items-center justify-center gap-2 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                搜索中…
              </div>
            ) : s.results.length === 0 ? (
              <div className="px-4 py-6 flex flex-col items-center gap-2 text-xs text-muted">
                {s.filter === 'all'
                  ? '未找到匹配的标的'
                  : `${ASSET_TYPE_LABELS[s.filter] ?? s.filter} 分类下无匹配结果`}
                {s.filter !== 'all' && (
                  <button
                    type="button"
                    onClick={() => { s.setFilter('all'); setActiveIdx(-1) }}
                    className="px-2 py-0.5 rounded-full text-[11px] text-accent hover:bg-accent/10 transition-colors"
                  >
                    在全部中搜索
                  </button>
                )}
              </div>
            ) : (
              <>
                {s.results.map((r, i) => (
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
                      const b = r.asset_type === 'stock' ? boardTag(r.symbol) : null
                      return b && (
                        <span className={`shrink-0 px-1 py-0.5 rounded text-[10px] leading-none border ${b.color}`}>{b.label}</span>
                      )
                    })()}
                    {r.asset_type === 'etf' && (
                      <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-violet-500/10 text-violet-400">ETF</span>
                    )}
                    {r.asset_type === 'index' && (
                      <span className="shrink-0 px-1 py-0.5 rounded text-[10px] leading-none bg-sky-500/10 text-sky-400">指数</span>
                    )}
                  </button>
                ))}

                {/* 触底哨兵: 进入下拉容器视口才加载下一页 */}
                {s.hasMore && <div ref={s.sentinelRef} className="h-px" />}

                {/* 触底加载 / 软上限提示 */}
                <div className="px-4 py-2 text-center text-[11px] text-muted">
                  {s.isFetchingNextPage
                    ? '加载中…'
                    : s.reachedCap
                      ? '结果过多，请输入更精确关键词'
                      : s.hasMore
                        ? '滚动到底部加载更多'
                        : '已显示全部结果'}
                </div>
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
