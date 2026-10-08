import { useState, useMemo, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { X, Plus, GripVertical, Upload, Loader2, Trash2, ListPlus } from 'lucide-react'
import { api, type StrategyDetail } from '@/lib/api'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

interface Props {
  pool: string[]
  onConfirm: (newPool: string[]) => void
  onClose: () => void
}

const SOURCE_CLS: Record<string, string> = {
  builtin: 'bg-accent/10 text-accent border-accent/20',
  custom: 'bg-amber-400/10 text-amber-400 border-amber-400/30',
  ai: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
  // 叠加策略归入「自定义」分组展示, 徽标与 StrategyCard 一致用 teal 区分
  composite: 'bg-teal-500/10 text-teal-400 border-teal-500/30',
  invalid: 'bg-danger/10 text-danger border-danger/20',
}

const SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  custom: '自定义',
  ai: 'AI',
  composite: '叠加',
  invalid: '失效',
}

const TF_BADGE_CLS = 'text-[8px] px-1 py-px rounded border leading-tight shrink-0 border-sky-500/30 bg-sky-500/10 text-sky-400'

type SourceTab = 'all' | 'builtin' | 'custom' | 'ai'

const TABS: { id: SourceTab; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'builtin', label: '内置' },
  { id: 'custom', label: '自定义' },
  { id: 'ai', label: 'AI' },
]

function parseMetaId(code: string): string {
  const m = code.match(/["']id["']\s*:\s*["']([A-Za-z0-9_-]+)["']/)
  return m ? m[1] : ''
}

function fileStem(name: string): string {
  return name.replace(/\.py$/i, '').replace(/[^A-Za-z0-9_-]/g, '_').replace(/^_+|_+$/g, '')
}

export function StrategyPoolDialog({ pool, onConfirm, onClose }: Props) {
  const backdrop = useDialogBackdrop(onClose)
  // 草稿状态: 打开时从 pool 复制, 操作只改草稿, 点确定才提交
  const [draftPool, setDraftPool] = useState<string[]>(() => [...pool])
  const [allStrategies, setAllStrategies] = useState<StrategyDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState<SourceTab>('all')
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState('')
  const [importMsg, setImportMsg] = useState('')
  const [publishingId, setPublishingId] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const loadStrategies = useCallback(async () => {
    setLoading(true)
    try {
      // 不按周期过滤: 日线+分钟策略合并展示, 分钟策略以徽章区分
      // include_research=true 同时拉取 research_only 草稿, 供 AI 标签「草稿」分区展示/发布
      const d = await api.strategyList(undefined, 'all', true)
      setAllStrategies(d.strategies)
    } catch {
      setAllStrategies([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadStrategies()
  }, [loadStrategies])

  const stratMap = useMemo(() => {
    const m = new Map<string, StrategyDetail>()
    allStrategies.forEach(s => m.set(s.id, s))
    return m
  }, [allStrategies])

  const validDraft = useMemo(
    () => draftPool.filter(id => stratMap.has(id)),
    [draftPool, stratMap]
  )
  const invalidPoolCount = draftPool.length - validDraft.length

  const available = useMemo(
    () => allStrategies.filter(s => !s.research_only && !draftPool.includes(s.id)),
    [allStrategies, draftPool]
  )

  // research_only 草稿(AI 来源)单独列出, 供「发布」操作; 不进待选列表。
  // 发布闸(/{id}/publish)仅接受 source=ai, 非 AI 的 research 项(如内置研究模板
  // factor_rank_research)混入会渲染出必然失败的「发布」按钮, 故此处排除。
  const drafts = useMemo(
    () => allStrategies.filter(s => s.research_only && s.source === 'ai'),
    [allStrategies]
  )

  // 按 Tab 分组过滤待选; 叠加策略(composite)并入「自定义」分组
  const filteredAvailable = useMemo(() => {
    if (activeTab === 'all') return available
    if (activeTab === 'custom') {
      return available.filter(s => s.source === 'custom' || s.source === 'composite')
    }
    return available.filter(s => s.source === activeTab)
  }, [available, activeTab])

  const handleAdd = useCallback((id: string) => {
    setDraftPool(prev => prev.includes(id) ? prev : [...prev, id])
  }, [])

  const handleRemove = useCallback((id: string) => {
    setDraftPool(prev => prev.filter(x => x !== id))
  }, [])

  const handleReorder = useCallback((newOrder: string[]) => {
    setDraftPool(newOrder)
  }, [])

  // 一键清空已选池 (草稿态, 误点可用「取消」恢复)
  const handleClearAll = useCallback(() => {
    setDraftPool([])
  }, [])

  // 一键加入当前 Tab 分组的全部待选策略
  const handleAddGroup = useCallback(() => {
    setDraftPool(prev => {
      const next = [...prev]
      for (const s of filteredAvailable) {
        if (!next.includes(s.id)) next.push(s.id)
      }
      return next
    })
  }, [filteredAvailable])

  // 发布 research_only 草稿 → 刷新后进入公开列表
  const handlePublish = useCallback(async (id: string) => {
    setPublishingId(id); setImportError(''); setImportMsg('')
    try {
      await api.strategyPublish(id)
      await loadStrategies()
      setImportMsg(`已发布: ${id}`)
    } catch (e: any) {
      setImportError(String(e?.message ?? '发布失败'))
    } finally {
      setPublishingId(null)
    }
  }, [loadStrategies])

  const handleImportFile = useCallback(async (file: File) => {
    setImporting(true); setImportError(''); setImportMsg('')
    try {
      if (!file.name.toLowerCase().endsWith('.py')) throw new Error('只能导入 .py 策略文件')
      const code = await file.text()
      const rawId = parseMetaId(code) || fileStem(file.name)
      if (!rawId) throw new Error('无法识别策略 ID，请检查 META.id 或文件名')
      const target: 'ai' | 'custom' = rawId.startsWith('ai_') ? 'ai' : 'custom'
      const strategyId = target === 'ai'
        ? rawId
        : (rawId.startsWith('custom_') ? rawId : `custom_${rawId}`)
      const result = await api.strategySaveCodeV2({
        strategy_id: strategyId,
        code,
        target_source: target,
        mode: 'create',
      })
      await loadStrategies()
      setActiveTab(result.source === 'ai' ? 'ai' : 'custom')
      const srcLabel = result.source === 'ai'
        ? (result.research_only ? 'AI 草稿(发布后可用)' : 'AI 策略')
        : '自定义策略'
      setImportMsg(`已导入到${srcLabel}: ${result.strategy_id}`)
    } catch (e: any) {
      setImportError(String(e?.message ?? '导入失败'))
    } finally {
      setImporting(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }, [loadStrategies])

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        {...backdrop}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 10 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 10 }}
          transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
          className="w-[680px] max-h-[78vh] bg-surface border border-border rounded-card shadow-xl flex flex-col"
        >
          {/* 标题 */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0">
            <span className="text-sm font-medium text-foreground">
              策略池 <span className="text-muted font-normal text-xs">{validDraft.length} / {allStrategies.length}</span>
              {invalidPoolCount > 0 && <span className="ml-2 text-[10px] text-danger">{invalidPoolCount} 个失效</span>}
            </span>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept=".py,text/x-python,text/plain"
                className="hidden"
                onChange={e => {
                  const file = e.target.files?.[0]
                  if (file) void handleImportFile(file)
                }}
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={importing}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn border border-accent/30 bg-accent/10 text-accent text-xs font-medium hover:bg-accent/15 disabled:opacity-50 transition-colors cursor-pointer"
              >
                {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}
                导入策略
              </button>
              <button onClick={onClose} className="p-1 rounded hover:bg-elevated transition-colors cursor-pointer">
                <X className="h-4 w-4 text-muted" />
              </button>
            </div>
          </div>

          {(importError || importMsg) && (
            <div className={`mx-4 mt-2 px-3 py-2 rounded-btn border text-[11px] shrink-0 ${importError ? 'border-danger/20 bg-danger/10 text-danger' : 'border-emerald-400/20 bg-emerald-400/10 text-emerald-400'}`}>
              {importError || importMsg}
            </div>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-16">
              <div className="w-5 h-5 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
            </div>
          ) : (
            <div className="flex-1 min-h-0 grid grid-cols-2 gap-0">
              {/* 左侧: 待选 (Tab 分组) */}
              <div className="flex flex-col min-h-0 border-r border-border">
                <div className="flex items-center gap-0.5 px-3 py-2 border-b border-border/60 shrink-0">
                  {TABS.map(tab => {
                    const count = tab.id === 'all'
                      ? available.length
                      : tab.id === 'custom'
                        ? available.filter(s => s.source === 'custom' || s.source === 'composite').length
                        : available.filter(s => s.source === tab.id).length
                    return (
                      <button
                        key={tab.id}
                        onClick={() => setActiveTab(tab.id)}
                        className={`px-2.5 py-1 text-[11px] font-medium rounded-btn transition-colors cursor-pointer ${
                          activeTab === tab.id
                            ? 'bg-accent/10 text-accent'
                            : 'text-muted hover:text-secondary hover:bg-elevated'
                        }`}
                      >
                        {tab.label}
                        <span className="ml-1 text-[9px] opacity-60">{count}</span>
                      </button>
                    )
                  })}
                  <button
                    onClick={handleAddGroup}
                    disabled={filteredAvailable.length === 0}
                    title="把当前分组剩余的策略全部加入策略池"
                    className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded-btn text-[10px] text-accent border border-accent/25 bg-accent/8 hover:bg-accent/15 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer shrink-0"
                  >
                    <ListPlus className="h-3 w-3" />
                    本组全加
                  </button>
                </div>
                <div className="flex-1 overflow-y-auto px-2 py-2">
                  {activeTab === 'ai' && drafts.length > 0 && (
                    <div className="mb-3">
                      <div className="flex items-center justify-between px-1 mb-1">
                        <span className="text-[10px] font-medium text-muted">草稿</span>
                        <span className="text-[9px] text-muted">{drafts.length} 个待发布</span>
                      </div>
                      <div className="space-y-0.5">
                        {drafts.map(s => (
                          <div
                            key={s.id}
                            className="flex items-center gap-2 px-2.5 py-1.5 rounded-btn border border-purple-500/15 bg-purple-500/5"
                          >
                            <span className="flex-1 min-w-0">
                              <span className="text-[12px] text-foreground block truncate">
                                {s.name} <span className="text-[10px] text-muted font-mono">{s.id}</span>
                              </span>
                              <span className="text-[10px] text-muted truncate block">{s.description}</span>
                            </span>
                            <button
                              onClick={() => handlePublish(s.id)}
                              disabled={publishingId === s.id}
                              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-btn text-[10px] text-purple-400 border border-purple-500/25 bg-purple-500/10 hover:bg-purple-500/20 disabled:opacity-50 transition-colors cursor-pointer shrink-0"
                            >
                              {publishingId === s.id && <Loader2 className="h-3 w-3 animate-spin" />}
                              发布
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {activeTab === 'ai' && drafts.length > 0 && (
                    <div className="px-1 mb-1 text-[10px] font-medium text-muted">待选</div>
                  )}
                  <div className="space-y-0.5">
                    {filteredAvailable.length === 0 ? (
                      <div className="flex items-center justify-center h-24 text-[11px] text-muted">
                        {available.length === 0 ? '全部已加入策略池' : '此分组无待选策略'}
                      </div>
                    ) : filteredAvailable.map(s => (
                      <button
                        key={s.id}
                        onClick={() => handleAdd(s.id)}
                        className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-btn
                          hover:bg-accent/8 transition-colors cursor-pointer group text-left"
                      >
                        <span className="flex-1 min-w-0">
                          <span className="text-[12px] text-foreground group-hover:text-accent transition-colors block truncate">
                            {s.name} <span className="text-[10px] text-muted font-mono">{s.id}</span>
                          </span>
                          <span className="text-[10px] text-muted truncate block">{s.description}</span>
                        </span>
                        <span className={`text-[8px] px-1 py-px rounded border leading-tight shrink-0 ${SOURCE_CLS[s.source] ?? SOURCE_CLS.builtin}`}>
                          {SOURCE_LABEL[s.source] ?? '内置'}
                        </span>
                        {s.timeframes?.includes('1m') && (
                          <span className={TF_BADGE_CLS}>分钟</span>
                        )}
                        <Plus className="h-3.5 w-3.5 text-muted/40 group-hover:text-accent shrink-0" />
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* 右侧: 已选 (Reorder.Group 纵向拖拽) */}
              <div className="flex flex-col min-h-0">
                <div className="flex items-center gap-1.5 px-3 py-2 border-b border-border/60 shrink-0">
                  <GripVertical className="h-3 w-3 text-muted/50" />
                  <span className="text-[10px] text-muted">已选 · 上下拖拽排序</span>
                  <button
                    onClick={handleClearAll}
                    disabled={draftPool.length === 0}
                    title="清空已选策略池 (未点确定前可用「取消」恢复)"
                    className="ml-auto inline-flex items-center gap-1 px-2 py-1 rounded-btn text-[10px] text-muted border border-border/60 hover:text-danger hover:border-danger/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer shrink-0"
                  >
                    <Trash2 className="h-3 w-3" />
                    清空
                  </button>
                </div>
                <div className="flex-1 overflow-y-auto px-2 py-2">
                  {draftPool.length === 0 ? (
                    <div className="flex items-center justify-center h-full text-[11px] text-muted">
                      从左侧点击策略添加
                    </div>
                  ) : (
                    <Reorder.Group
                      axis="y"
                      values={draftPool}
                      onReorder={handleReorder}
                      className="space-y-1"
                    >
                      {draftPool.map(id => {
                        const s = stratMap.get(id)
                        const src = s?.source ?? 'invalid'
                        return (
                          <Reorder.Item
                            key={id}
                            value={id}
                            className="flex items-center gap-2 px-2.5 py-1.5 rounded-btn
                              bg-accent/8 border border-accent/20
                              cursor-grab active:cursor-grabbing
                              hover:bg-accent/15 transition-colors group"
                            whileDrag={{ scale: 1.02, zIndex: 50, boxShadow: '0 4px 12px rgba(0,0,0,0.2)' }}
                          >
                            <GripVertical className="h-3.5 w-3.5 text-accent/40 group-hover:text-accent/70 shrink-0" />
                            <span className="flex-1 min-w-0 text-[12px] text-foreground truncate">
                              {s?.name ?? id} <span className="text-[10px] text-muted font-mono">{id}</span>
                            </span>
                            <span className={`text-[8px] px-1 py-px rounded border leading-tight shrink-0 ${SOURCE_CLS[src] ?? SOURCE_CLS.builtin}`}>
                              {SOURCE_LABEL[src] ?? '内置'}
                            </span>
                            {s?.timeframes?.includes('1m') && (
                              <span className={TF_BADGE_CLS}>分钟</span>
                            )}
                            <button
                              onClick={(e) => { e.stopPropagation(); handleRemove(id) }}
                              className="text-muted/40 hover:text-danger transition-colors cursor-pointer leading-none shrink-0"
                              title="移除"
                            >
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </Reorder.Item>
                        )
                      })}
                    </Reorder.Group>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 底部 */}
          <div className="flex items-center justify-between px-4 py-2 border-t border-border shrink-0">
            <span className="text-[10px] text-muted">仅策略池中的策略会在扫描时运行</span>
            <div className="flex items-center gap-2">
              <button
                onClick={onClose}
                className="px-3 py-1 text-xs rounded-btn border border-border text-muted hover:text-foreground hover:border-border/80 transition-colors cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={() => { onConfirm(draftPool); onClose() }}
                className="px-3 py-1 text-xs rounded-btn bg-accent text-white hover:bg-accent/90 transition-colors cursor-pointer"
              >
                确定
              </button>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
