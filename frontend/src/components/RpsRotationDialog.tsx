import { useState, useMemo, useRef, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Repeat, Sparkles, ArrowDownUp, RefreshCw, AlertCircle } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct } from '@/lib/format'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { Modal } from '@/components/Modal'

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

// 排名 → 前景色(A 股语义: 红=强, 绿=弱)。前 10 红, 后 10 绿, 中间默认强调色。
// total 兜底: 概念总数未知时只判前 10, 不判后 10。
function rankColorClass(rank: number, total: number): string {
  if (rank <= 10) return 'text-bull'
  if (total > 20 && rank > total - 10) return 'text-bear'
  return 'text-accent'
}

export function RpsRotationDialog({ onClose }: Props) {
  const [days, setDays] = useState(DEFAULT_DAYS)
  const [reversed, setReversed] = useState(false)        // false=高→低, true=低→高
  const [selected, setSelected] = useState<string | null>(null)  // 点中的概念名, 高亮追踪

  // ---- AI 轮动分析状态 (组件内, 不建全局 store: 切页即关对话框) ----
  const [analysis, setAnalysis] = useState('')            // 累积的 Markdown 报告
  const [analyzing, setAnalyzing] = useState(false)       // 生成中
  const [analysisError, setAnalysisError] = useState('')  // 错误信息
  const [analysisMeta, setAnalysisMeta] = useState<{ summary?: string } | null>(null)
  const [focus, setFocus] = useState('')                  // 用户追加的关注点

  const runAnalysis = useCallback(async (daysParam: number, focusParam: string) => {
    setAnalyzing(true)
    setAnalysis('')
    setAnalysisError('')
    setAnalysisMeta(null)
    try {
      for await (const ev of api.rotationAnalyzeStream(daysParam, focusParam)) {
        if (ev.type === 'meta') setAnalysisMeta({ summary: ev.summary })
        else if (ev.type === 'delta') setAnalysis(a => a + (ev.content ?? ''))
        else if (ev.type === 'error') setAnalysisError(ev.message ?? '未知错误')
        // done: 无操作
      }
    } catch (e) {
      setAnalysisError(e instanceof Error ? e.message : String(e))
    } finally {
      setAnalyzing(false)
    }
  }, [])

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
  // AI 报告区滚动容器: 流式生成时自动滚到底部
  const analysisRef = useRef<HTMLDivElement>(null)
  const [visibleRange, setVisibleRange] = useState({ start: 0, end: 25 })

  // 流式生成中: analysis 每次追加都把报告区滚到底部, 跟踪最新文字
  useEffect(() => {
    if (!analyzing) return
    const el = analysisRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [analysis, analyzing])

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


  // 选中概念的追踪行: 找出它在每个日期列的(排名, 涨幅)。
  // 每列已按涨幅降序排好, 故排名 = 该概念在数组里的索引 + 1。
  // 未入选该日(概念当天无数据)显示空, 便于横向看排名变化。
  const selectedRow = useMemo(() => {
    if (!selected) return null
    const cells: ({ rank: number; pct: number } | null)[] = []
    for (const d of dates) {
      const col = columns[d] ?? []
      const idx = col.findIndex(([name]) => name === selected)
      cells.push(idx >= 0 ? { rank: idx + 1, pct: col[idx][1] } : null)
    }
    return cells
  }, [selected, dates, columns])

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
    <Modal
      onClose={onClose}
      labelledBy="rps-rotation-title"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      panelClassName="w-[92vw] max-w-[1100px] h-[88vh] bg-surface border border-border rounded-card shadow-xl flex flex-col"
    >
          {/* 标题栏 */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              <Repeat className="h-4 w-4 text-accent" />
              <span id="rps-rotation-title" className="text-sm font-medium text-foreground">概念涨幅轮动</span>
              <span className="text-[11px] text-muted">
                {conceptCount > 0 ? `${dates.length} 天 · ${conceptCount} 个概念` : '暂无数据'}
              </span>
            </div>
            <button aria-label="关闭" onClick={onClose} className="p-1 rounded hover:bg-elevated transition-colors cursor-pointer">
              <X className="h-4 w-4 text-muted" />
            </button>
          </div>

          {/* 上半区: AI 轮动分析 */}
          <div className="shrink-0 border-b border-border flex flex-col max-h-[42%]">
            {/* 标题栏: 标题 + meta 摘要 + focus 输入 + 触发按钮 */}
            <div className="flex items-center gap-2 px-4 py-1.5 bg-elevated/30 shrink-0">
              <Sparkles className={cn('h-3.5 w-3.5 text-accent/60', analyzing && 'animate-pulse')} />
              <span className="text-[11px] text-muted shrink-0">AI 轮动分析</span>
              {analysisMeta?.summary && (
                <span className="text-[11px] text-accent/80 truncate">{analysisMeta.summary}</span>
              )}
              <div className="flex items-center gap-1.5 ml-auto">
                <input
                  type="text"
                  value={focus}
                  onChange={e => setFocus(e.target.value)}
                  placeholder="关注点(可选)"
                  disabled={analyzing}
                  className="w-28 px-2 py-0.5 text-[11px] bg-elevated/50 border border-border rounded-btn text-foreground placeholder:text-muted/50 focus:outline-none focus:border-accent/40 disabled:opacity-50"
                />
                <button
                  onClick={() => runAnalysis(days, focus)}
                  disabled={analyzing}
                  className={cn(
                    'inline-flex items-center gap-1 px-2 py-0.5 rounded-btn text-[11px] transition-colors cursor-pointer border',
                    analyzing
                      ? 'opacity-60 cursor-not-allowed border-border text-muted'
                      : 'bg-accent/10 text-accent border-accent/30 hover:bg-accent/20',
                  )}
                >
                  {analyzing
                    ? <><RefreshCw className="h-3 w-3 animate-spin" />分析中</>
                    : analysis
                      ? <><RefreshCw className="h-3 w-3" />重新分析</>
                      : <><Sparkles className="h-3 w-3" />生成分析</>}
                </button>
              </div>
            </div>

            {/* 报告内容区: 四态渲染 */}
            <div ref={analysisRef} className="flex-1 min-h-0 overflow-auto">
              {analysisError ? (
                <div className="flex items-center gap-2 px-4 py-4 text-[11px] text-danger">
                  <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                  <span>{analysisError}</span>
                  <button
                    onClick={() => runAnalysis(days, focus)}
                    className="ml-auto text-accent hover:underline shrink-0"
                  >重试</button>
                </div>
              ) : analysis || analyzing ? (
                <div className="px-4 py-2.5 text-[12px] leading-relaxed">
                  <MarkdownRenderer content={analysis} />
                  {analyzing && (
                    <span className="inline-block w-1.5 h-3.5 bg-accent animate-pulse align-middle ml-0.5" />
                  )}
                </div>
              ) : (
                <div className="px-4 py-4 text-center text-[11px] text-muted/60">
                  点击「生成分析」,AI 将从主线研判 / 新晋强势 / 退潮预警 / 机构vs游资 等角度分析最近 {days} 天的概念轮动
                </div>
              )}
            </div>
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
                    {/* 选中概念追踪行: 在日期表头下方单独一行, 横向展示它在各日的排名+涨幅 */}
                    <AnimatePresence>
                      {selected && selectedRow && (
                        <motion.tr
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 1, height: 'auto' }}
                          exit={{ opacity: 0, height: 0 }}
                          transition={{ duration: 0.15 }}
                          className="border-b border-accent/20 bg-accent/5"
                        >
                          <td className="sticky left-0 z-30 bg-surface px-2 py-1 text-center border-r border-border/40">
                            <span className="text-[10px] text-accent truncate block max-w-[44px]" title={selected}>
                              {selected}
                            </span>
                          </td>
                          {selectedRow.map((cell, i) => (
                            <td key={i} className="px-2 py-1 text-center whitespace-nowrap align-middle">
                              {cell ? (
                                <div className="flex flex-col items-center gap-0.5 leading-tight">
                                  <span className={cn(
                                    'text-[11px] font-medium tabular-nums',
                                    rankColorClass(cell.rank, conceptCount),
                                  )}>
                                    #{cell.rank}
                                  </span>
                                  <span className={cn(
                                    'text-[10px] tabular-nums',
                                    cell.pct > 0 ? 'text-bull' : cell.pct < 0 ? 'text-bear' : 'text-muted',
                                  )}>
                                    {fmtPct(cell.pct)}
                                  </span>
                                </div>
                              ) : (
                                <span className="text-[10px] text-muted/40">—</span>
                              )}
                            </td>
                          ))}
                        </motion.tr>
                      )}
                    </AnimatePresence>
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
    </Modal>
  )
}
