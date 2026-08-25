import { useState, useMemo, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Repeat, ArrowDownUp, Search } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct } from '@/lib/format'

interface Props {
  onClose: () => void
}

const DEFAULT_DAYS = 12
const ROW_HEIGHT = 30        // 每行高度(px), 与单元格样式配合
const OVERSCAN = 8           // 上下额外渲染行数, 减少滚动时的白屏闪烁
const MIN_DAYS = 7
const MAX_DAYS = 30

// 涨幅 → 背景色梯度(A 股语义: 红涨绿跌)。强度越大色越深, 一眼看出强势/弱势概念
function pctBgClass(pct: number): string {
  if (pct >= 0.05) return 'bg-bull/25'
  if (pct >= 0.03) return 'bg-bull/18'
  if (pct >= 0.01) return 'bg-bull/10'
  if (pct > -0.01) return ''
  if (pct > -0.03) return 'bg-bear/10'
  if (pct > -0.05) return 'bg-bear/18'
  return 'bg-bear/25'
}

// 把 "2026-07-01" 格式化成 "7/01" 紧凑显示(表头窄列)
function shortDate(s: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  if (!m) return s
  return `${Number(m[2])}/${m[3]}`
}

export function RpsRotationDialog({ onClose }: Props) {
  const [days, setDays] = useState(DEFAULT_DAYS)
  const [reversed, setReversed] = useState(false)        // false=高→低, true=低→高
  const [selected, setSelected] = useState<string | null>(null)  // 点中的概念名, 高亮追踪
  const [search, setSearch] = useState('')

  // 数据请求: React Query 缓存, 同 days 5 分钟内重开秒开
  const { data, isLoading, error } = useQuery({
    queryKey: QK.rpsRotation(days),
    queryFn: () => api.rpsRotation(days),
    staleTime: 5 * 60 * 1000,
  })

  const dates = data?.dates ?? []
  const columns = data?.columns ?? {}
  const conceptCount = data?.concept_count ?? 0

  // 行数 = 最长那列的长度(理论上每天概念数应一致, 取最大兜底)
  const rowCount = useMemo(
    () => dates.reduce((m, d) => Math.max(m, columns[d]?.length ?? 0), 0),
    [dates, columns],
  )

  // 行索引: 翻转时不重排数据, 只翻转访问索引(省一次大数组操作)
  const getRowIndex = useCallback(
    (displayIdx: number) => (reversed ? rowCount - 1 - displayIdx : displayIdx),
    [reversed, rowCount],
  )

  // ---- 手写虚拟滚动 ----
  // 监听滚动容器 scrollTop, 只渲染 [firstIdx, lastIdx] 范围内的行。
  // 387 行只画可视的 ~25 行 + overscan, DOM 恒定 ~30 行 × N 列, 滚动 60fps。
  const scrollRef = useRef<HTMLDivElement>(null)
  const [visibleRange, setVisibleRange] = useState({ start: 0, end: 25 })

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const scrollTop = el.scrollTop
    const viewportH = el.clientHeight
    const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN)
    const end = Math.min(rowCount, Math.ceil((scrollTop + viewportH) / ROW_HEIGHT) + OVERSCAN)
    setVisibleRange(prev => (prev.start === start && prev.end === end ? prev : { start, end }))
  }, [rowCount])

  useEffect(() => {
    // rowCount 变化(切天数/数据到达)时重算可视范围
    handleScroll()
  }, [handleScroll, rowCount])

  // ESC 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // 搜索命中: 找出该概念在(未翻转的)每列中的排名, 用于跳转高亮
  // 仅在有搜索词时计算, 避免每次渲染都遍历
  const searchMatch = useMemo(() => {
    const q = search.trim()
    if (!q || rowCount === 0) return null
    // 在最新日期列里找第一个含搜索词的概念, 返回它的显示行号(考虑翻转)
    const latest = dates[0]
    const col = columns[latest] ?? []
    const rawIdx = col.findIndex(([name]) => name.includes(q))
    if (rawIdx < 0) return null
    return reversed ? rowCount - 1 - rawIdx : rawIdx
  }, [search, columns, dates, reversed, rowCount])

  // 搜索命中时自动滚到该行
  useEffect(() => {
    if (searchMatch == null) return
    const el = scrollRef.current
    if (el) el.scrollTo({ top: searchMatch * ROW_HEIGHT - el.clientHeight / 2, behavior: 'smooth' })
  }, [searchMatch])

  const renderRows = useMemo(() => {
    const rows: JSX.Element[] = []
    for (let displayIdx = visibleRange.start; displayIdx < visibleRange.end; displayIdx++) {
      const rawIdx = getRowIndex(displayIdx)
      const cells = dates.map((d) => {
        const cell = columns[d]?.[rawIdx]
        if (!cell) {
          return (
            <td key={d} className="px-2 py-1 text-center text-muted/40">
              <span className="text-[10px]">—</span>
            </td>
          )
        }
        const [name, pct] = cell
        const isSelected = selected === name
        return (
          <td
            key={d}
            onClick={() => setSelected(prev => prev === name ? null : name)}
            className={cn(
              'px-2 py-1 cursor-pointer whitespace-nowrap text-center align-middle transition-colors',
              pctBgClass(pct),
              isSelected && 'ring-1 ring-inset ring-accent bg-accent/20',
            )}
          >
            <div className="flex flex-col items-center gap-0.5 leading-tight">
              <span className={cn(
                'text-[11px] max-w-[84px] truncate',
                isSelected ? 'text-accent font-medium' : 'text-secondary',
              )} title={name}>{name}</span>
              <span className={cn(
                'text-[10px] tabular-nums',
                pct > 0 ? 'text-bull' : pct < 0 ? 'text-bear' : 'text-muted',
              )}>{fmtPct(pct)}</span>
            </div>
          </td>
        )
      })
      rows.push(
        <tr
          key={displayIdx}
          style={{ height: ROW_HEIGHT }}
          className="border-b border-border/30"
        >
          <td className="sticky left-0 z-10 bg-surface px-2 text-center text-[10px] text-muted tabular-nums border-r border-border/40">
            {displayIdx + 1}
          </td>
          {cells}
        </tr>,
      )
    }
    return rows
  }, [visibleRange, getRowIndex, dates, columns, selected])

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        onClick={e => { if (e.target === e.currentTarget) onClose() }}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
          className="w-[92vw] max-w-[1100px] h-[88vh] bg-surface border border-border rounded-card shadow-xl flex flex-col"
        >
          {/* 标题栏 */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              <Repeat className="h-4 w-4 text-accent" />
              <span className="text-sm font-medium text-foreground">概念涨幅轮动</span>
              <span className="text-[11px] text-muted">
                {conceptCount > 0 ? `${dates.length} 天 · ${conceptCount} 个概念` : '暂无数据'}
              </span>
            </div>
            <button onClick={onClose} className="p-1 rounded hover:bg-elevated transition-colors cursor-pointer">
              <X className="h-4 w-4 text-muted" />
            </button>
          </div>


          {/* 工具栏 */}
          <div className="flex items-center gap-3 px-4 py-2 border-b border-border shrink-0">
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-muted">天数</span>
              <input
                type="range"
                min={MIN_DAYS}
                max={MAX_DAYS}
                step={1}
                value={days}
                onChange={e => setDays(Number(e.target.value))}
                className="w-24 accent-accent cursor-pointer"
              />
              <span className="text-[11px] text-secondary tabular-nums w-5">{days}</span>
            </div>
            <button
              onClick={() => setReversed(r => !r)}
              className={cn(
                'inline-flex items-center gap-1 px-2 py-1 rounded-btn text-[11px] transition-colors cursor-pointer border',
                reversed
                  ? 'bg-accent/10 text-accent border-accent/30'
                  : 'border-border text-muted hover:text-secondary hover:bg-elevated',
              )}
              title="翻转排序(高↔低)"
            >
              <ArrowDownUp className="h-3 w-3" />
              {reversed ? '低→高' : '高→低'}
            </button>
            <div className="relative flex-1 max-w-[220px] ml-auto">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted/50" />
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="搜索概念定位…"
                className="w-full pl-7 pr-2 py-1 text-[11px] bg-elevated/50 border border-border rounded-btn text-foreground placeholder:text-muted/50 focus:outline-none focus:border-accent/40"
              />
            </div>
            {selected && (
              <button
                onClick={() => setSelected(null)}
                className="text-[11px] text-accent hover:underline cursor-pointer"
              >
                取消追踪「{selected}」
              </button>
            )}
          </div>

          {/* 下半区: 涨幅轮动矩阵(虚拟滚动) */}
          <div className="flex-1 min-h-0 flex flex-col">
            {isLoading ? (
              <div className="flex items-center justify-center py-16">
                <div className="w-5 h-5 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
              </div>
            ) : error ? (
              <div className="flex items-center justify-center py-16 text-[11px] text-danger">
                加载失败,请稍后重试
              </div>
            ) : rowCount === 0 ? (
              <div className="flex items-center justify-center py-16 text-[11px] text-muted">
                暂无概念数据,请先在「概念分析」页配置并获取概念数据源
              </div>
            ) : (
              <div
                ref={scrollRef}
                onScroll={handleScroll}
                className="flex-1 overflow-auto"
              >
                <table className="min-w-full border-collapse">
                  {/* 表头: 日期列, 最新在最左 */}
                  <thead className="sticky top-0 z-20 bg-surface">
                    <tr>
                      <th className="sticky left-0 z-30 bg-surface px-2 py-1.5 text-[10px] font-normal text-muted border-b border-r border-border/40">
                        #
                      </th>
                      {dates.map(d => (
                        <th
                          key={d}
                          className="px-2 py-1.5 text-[10px] font-normal text-muted border-b border-border/40 whitespace-nowrap text-center"
                          title={d}
                        >
                          {shortDate(d)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {/* 顶部占位: 把滚动位置撑起来 */}
                    {visibleRange.start > 0 && (
                      <tr style={{ height: visibleRange.start * ROW_HEIGHT }}>
                        <td colSpan={dates.length + 1} />
                      </tr>
                    )}
                    {renderRows}
                    {/* 底部占位 */}
                    {visibleRange.end < rowCount && (
                      <tr style={{ height: (rowCount - visibleRange.end) * ROW_HEIGHT }}>
                        <td colSpan={dates.length + 1} />
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* 底部提示 */}
          <div className="px-4 py-1.5 border-t border-border shrink-0">
            <span className="text-[10px] text-muted">
              每列各自按当日涨幅排序 · 点击单元格追踪概念在各日的排名变化
            </span>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
