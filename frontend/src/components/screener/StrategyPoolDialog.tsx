import { useState, useMemo, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { X, Plus, GripVertical, Upload, Loader2 } from 'lucide-react'
import { api, type StrategyDetail } from '@/lib/api'

interface Props {
  pool: string[]
  onConfirm: (newPool: string[]) => void
  onClose: () => void
}

const SOURCE_CLS: Record<string, string> = {
  builtin: 'bg-accent/10 text-accent border-accent/20',
  custom: 'bg-amber-400/10 text-amber-400 border-amber-400/30',
  ai: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
  invalid: 'bg-danger/10 text-danger border-danger/20',
}

const SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  custom: '自定义',
  ai: 'AI',
  invalid: '失效',
}

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
  // 草稿状态: 打开时从 pool 复制, 操作只改草稿, 点确定才提交
  const [draftPool, setDraftPool] = useState<string[]>(() => [...pool])
  const [allStrategies, setAllStrategies] = useState<StrategyDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState<SourceTab>('all')
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState('')
  const [importMsg, setImportMsg] = useState('')
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const loadStrategies = useCallback(async () => {
    setLoading(true)
    try {
      const d = await api.strategyList()
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
    () => allStrategies.filter(s => !draftPool.includes(s.id)),
    [allStrategies, draftPool]
  )

  // 按 Tab 分组过滤待选
  const filteredAvailable = useMemo(() => {
    if (activeTab === 'all') return available
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
      setActiveTab(result.source)
      setImportMsg(`已导入到${result.source === 'ai' ? 'AI' : '自定义'}策略: ${result.strategy_id}`)
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
        onClick={e => { if (e.target === e.currentTarget) onClose() }}
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
                </div>
                <div className="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
                  {filteredAvailable.length === 0 ? (
                    <div className="flex items-center justify-center h-full text-[11px] text-muted">
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
                      <Plus className="h-3.5 w-3.5 text-muted/40 group-hover:text-accent shrink-0" />
                    </button>
                  ))}
                </div>
              </div>

              {/* 右侧: 已选 (Reorder.Group 纵向拖拽) */}
              <div className="flex flex-col min-h-0">
                <div className="flex items-center gap-1.5 px-3 py-2 border-b border-border/60 shrink-0">
                  <GripVertical className="h-3 w-3 text-muted/50" />
                  <span className="text-[10px] text-muted">已选 · 上下拖拽排序</span>
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
