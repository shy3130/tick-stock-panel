import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Sparkles, Save, Loader2, ChevronLeft, ChevronRight, AlertTriangle, Settings2, FileText, Copy, Check, Terminal, Play } from 'lucide-react'
import { api } from '@/lib/api'
import { storage } from '@/lib/storage'
import { cn } from '@/lib/cn'
import { AiProviderSelector } from '@/components/AiProviderSelector'
import { resolveEntryProfile } from '@/lib/aiProfile'
import { stageScreenerBacktestHandoff, peekScreenerBacktestHandoff } from '@/lib/screenerBacktestHandoff'
import { toast } from '@/components/Toast'

// ===== 工具函数 =====

function parsePyValue(v: string): any {
  const s = v.trim()
  if (s === 'True') return true
  if (s === 'False') return false
  if (s === 'None') return null
  return JSON.parse(s)
}

function slugId(): string {
  return 'ai_' + Date.now().toString(36)
}

function parseParams(code: string): any[] {
  const m = code.match(/"params"\s*:\s*\[([^\]]*)\]/)
  if (!m) return []
  const inner = m[1]
  const blocks = inner.match(/\{[^}]+\}/g) ?? []
  return blocks.map(b => {
    const get = (key: string) => {
      const re = new RegExp('"' + key + '"\\s*:\\s*(.+?)\\s*[,}]')
      const r = b.match(re)
      return r ? r[1].trim().replace(/^"(.*)"$/, '$1') : ''
    }
    const id = get('id'); if (!id) return null
    const type = get('type')
    return { id, type, label: get('label'), default: parsePyValue(get('default') || 'null'), min: parsePyValue(get('min') || 'null'), max: parsePyValue(get('max') || 'null'), step: parsePyValue(get('step') || 'null') }
  }).filter(Boolean)
}

function parseStringList(code: string, key: string): string[] {
  const re = new RegExp(key + '\\s*=\\s*\\[([^\\]]+)\\]')
  const m = code.match(re)
  if (!m) return []
  const items = m[1].match(/"([^"]+)"/g)
  return items ? items.map(x => x.replace(/"/g, '')) : []
}

function parseScoring(code: string): Record<string, number> {
  const m = code.match(/"scoring"\s*:\s*\{([^}]+)\}/)
  if (!m) return {}
  const items = m[1].match(/"([^"]+)"\s*:\s*([0-9.]+)/g)
  if (!items) return {}
  const result: Record<string, number> = {}
  for (const item of items) {
    const p = item.match(/"([^"]+)"\s*:\s*([0-9.]+)/)
    if (p) result[p[1]] = parseFloat(p[2])
  }
  return result
}

function parseRules(code: string): string {
  const m = code.match(/RULES\s*=\s*"""\s*([\s\S]*?)\s*"""/)
  return m ? m[1].trim() : ''
}

function parseMetaField(code: string, field: string): string {
  const m = code.match(new RegExp('"' + field + '"\\s*:\\s*"([^"]+)"'))
  return m ? m[1] : ''
}

// ===== 常量 =====

const DIRECTIONS = [
  { value: 'long', label: '做多' },
  { value: 'short', label: '做空' },
  { value: 'monitor', label: '监控' },
]

// ===== 组件 =====

const CUSTOM_TEMPLATE = `"""策略简短描述"""
import polars as pl

META = {
    "id": "custom_my_strategy",
    "name": "我的策略",
    "description": "策略描述",
    "tags": ["自定义"],
    "basic_filter": {
        "price_min": 3, "price_max": 200,
        "market_cap_min": 10e8, "amount_min": 0.5e8,
        "exclude_st": True, "exclude_new_days": 30,
    },
    "params": [],
    "scoring": {
        "change_pct": 0.5, "vol_ratio_5d": 0.5,
    },
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_n_day_high"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20
ALERTS = []

RULES = """
1. 规则一
2. 规则二
3. 规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return (
        (pl.col("close") > pl.col("ma20"))
        & (pl.col("volume") > pl.col("vol_ma5") * 1.5)
    )
`

interface Props { open: boolean; onClose: () => void; onSavedId?: (id: string) => void | Promise<void>; mode?: 'create' | 'modify' }

export function StrategyBuilderDialog({ open, onClose, onSavedId, mode = 'create' }: Props) {
  // 根据 mode 选择存储 key
  const draftStore = mode === 'modify' ? storage.strategyModify : storage.strategyDraft
  const navigate = useNavigate()
  const [step, setStep] = useState(1)
  const [tab, setTab] = useState<'ai' | 'custom'>('ai')
  const [previewTab, setPreviewTab] = useState<'params' | 'code'>('params')
  const [customCopied, setCustomCopied] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [direction, setDirection] = useState('long')
  const [rules, setRules] = useState('')
  const [code, setCode] = useState('')
  const [instruction, setInstruction] = useState('')
  const [error, setError] = useState('')
  const [savedId, setSavedId] = useState<string | null>(null)
  const [strategyId, setStrategyId] = useState('')

  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [aiStatus, setAiStatus] = useState<{ configured: boolean } | null>(null)
  const [checkedAi, setCheckedAi] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [profileId, setProfileId] = useState<string>()
  const aiProfiles = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })

  const selectedProfileId = () =>
    resolveEntryProfile('strategy_build', aiProfiles.data?.profiles ?? [], aiProfiles.data?.default_id ?? '') || profileId

  // 打开时恢复草稿
  useEffect(() => {
    if (!open) { setLoaded(false); setSavedId(null); return }
    const d = draftStore.get(null)
    if (d) {
      setStep(d.step ?? 1); setName(d.name ?? ''); setDescription(d.description ?? '')
      setDirection(d.direction ?? 'long')
      setRules(d.rules ?? ''); setCode(d.code ?? ''); setStrategyId(d.strategyId ?? '')
    }
    setLoaded(true)
  }, [open])

  // 打开时检查 AI 状态
  useEffect(() => {
    if (!open || checkedAi) return
    api.strategyAiStatus().then(s => { setAiStatus(s); setCheckedAi(true) }).catch(() => setAiStatus({ configured: false }))
  }, [open, checkedAi])

  // 持久化
  const persist = useCallback(() => {
    if (!name && !rules && !code) {
      draftStore.set(null)
    } else {
      draftStore.set({ name, description, direction, rules, code, step, strategyId })
    }
  }, [name, description, direction, rules, code, step, strategyId])
  useEffect(() => { if (loaded) persist() }, [loaded, persist])

  const clearDraft = () => {
    setName(''); setDescription(''); setDirection('long')
    setRules(''); setCode(''); setStep(1); setError(''); setInstruction('')
    setStrategyId('')
  }

  const handleClose = () => { if (name || rules || code) persist(); onClose() }

  const handleGenerate = async () => {
    if (!name.trim() || !rules.trim()) return
    if (!aiStatus?.configured) { setError('AI 未配置，请在设置页面配置 API Key'); return }
    setLoading(true); setError('')
    try {
      const id = strategyId || slugId()
      setStrategyId(id)
      const res = await api.strategyBuild(1, { name: name.trim(), description: description.trim(), direction, rules: rules.trim(), strategy_id: id }, selectedProfileId())
      if (!res.valid) { setError(res.error ?? '生成失败'); return }
      setCode(res.code); setStep(2)
      const genDesc = parseMetaField(res.code, 'description')
      const genRules = parseRules(res.code)
      if (genDesc) setDescription(genDesc)
      if (genRules) setRules(genRules)
    } catch (e: any) {
      const msg = String(e?.message ?? '')
      setError(msg.includes('API Key') || msg.includes('api_key') ? 'AI API Key 未配置或无效' : (msg || '生成失败'))
    } finally { setLoading(false) }
  }

  const handleModify = async () => {
    if (!instruction.trim() || !code) return
    setLoading(true); setError('')
    try {
      const res = await api.strategyBuild(2, { current_code: code, instruction: instruction.trim() }, selectedProfileId())
      if (!res.valid) { setError(res.error ?? '修改失败'); return }
      setCode(res.code); setInstruction('')
      const genDesc = parseMetaField(res.code, 'description')
      const updatedRules = parseRules(res.code)
      if (genDesc) setDescription(genDesc)
      if (updatedRules) setRules(updatedRules)
    } catch (e: any) { setError(String(e?.message ?? '修改失败')) }
    finally { setLoading(false) }
  }

  // 手动保存
  const handleSave = async () => {
    if (!code) return
    setSaving(true)
    try {
      const idMatch = code.match(/"id"\s*:\s*"([^"]+)"/)
      const id = idMatch?.[1] || strategyId || slugId()
      await api.strategySaveCode(id, code)
      const genRules = parseRules(code)
      const finalRules = (genRules || rules).trim()
      if (finalRules) { const saved = storage.strategyRules.get({}); saved[id] = finalRules; storage.strategyRules.set(saved) }
      await onSavedId?.(id)
      clearDraft()
      // F12: 保存成功后停留在弹窗内展示成功态, 不再 1s 自动关闭
      setSavedId(id)
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败') }
    setSaving(false)
  }

  // F12: 保存成功后「回测此策略」— 纯策略交接 (空池 + strategyId)
  const sendToBacktest = () => {
    if (!savedId) return
    stageScreenerBacktestHandoff({ target: 'strategy', symbols: [], asOf: null, strategyId: savedId })
    // 纯策略交接 stage 返回 0 (标的数), 不能当失败信号; 用 peek 复核是否真正写入
    if (peekScreenerBacktestHandoff()?.strategyId !== savedId) {
      toast('暂存交接失败，请重试', 'error')
      return
    }
    navigate('/backtest')
  }

  if (!open) return null

  const params = parseParams(code)
  const entrySignals = parseStringList(code, 'ENTRY_SIGNALS')
  const exitSignals = parseStringList(code, 'EXIT_SIGNALS')
  const scoring = parseScoring(code)
  const stopMatch = code.match(/STOP_LOSS\s*=\s*(-?[0-9.]+|None)/)
  const codeStopLoss = stopMatch ? (stopMatch[1] === 'None' ? null : parseFloat(stopMatch[1])) : null
  const holdMatch = code.match(/MAX_HOLD_DAYS\s*=\s*(-?[0-9.]+|None)/)
  const codeHoldDays = holdMatch ? (holdMatch[1] === 'None' ? null : parseInt(holdMatch[1])) : null
  const hasParams = params.length > 0 || entrySignals.length > 0 || exitSignals.length > 0 || Object.keys(scoring).length > 0

  return (
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
        onClick={e => { if (e.target === e.currentTarget) handleClose() }}>
        <motion.div initial={{ opacity: 0, scale: 0.95, y: 10 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.95, y: 10 }}
          className="w-[820px] max-h-[88vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden">

          {/* 标题 */}
          <div className="grid grid-cols-[1fr_auto_1fr] items-center px-5 py-2.5 border-b border-border/50">
            {/* 左侧：Tab 切换 */}
            <div className="flex rounded-lg bg-elevated p-0.5 w-fit">
              <button onClick={() => setTab('ai')} className={cn('px-3 py-1 rounded-md text-xs font-medium transition-all cursor-pointer', tab === 'ai' ? 'bg-amber-400/15 text-amber-400' : 'text-muted hover:text-foreground')}>
                <Sparkles className="h-3 w-3 inline mr-1" />AI 生成
              </button>
              <button onClick={() => setTab('custom')} className={cn('px-3 py-1 rounded-md text-xs font-medium transition-all cursor-pointer', tab === 'custom' ? 'bg-accent/15 text-accent' : 'text-muted hover:text-foreground')}>
                <FileText className="h-3 w-3 inline mr-1" />自定义编写
              </button>
            </div>
            {/* 中间：标题 */}
            <span className="text-sm font-semibold text-foreground">
              {strategyId ? '修改策略' : '创建策略'}
            </span>
            {/* 右侧：步骤 + 关闭 */}
            <div className="flex items-center justify-end gap-2">
              {tab === 'ai' && (
                <div className="flex items-center gap-1">
                  <span className={'w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold ' + (step === 1 ? 'bg-amber-400/20 text-amber-400' : 'bg-emerald-400/20 text-emerald-400')}>1</span>
                  <span className="text-muted/20 text-[10px]">—</span>
                  <span className={'w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold ' + (step === 2 ? 'bg-amber-400/20 text-amber-400' : 'bg-border/50 text-muted')}>2</span>
                </div>
              )}
              <button onClick={handleClose} className="p-1.5 rounded-lg hover:bg-elevated"><X className="h-4 w-4 text-muted" /></button>
            </div>
          </div>

          {/* Tab 描述 */}
          <div className="px-5 py-2 border-b border-border/30 bg-elevated/30">
            {tab === 'ai' ? (
              <div className="flex items-center gap-2 text-[11px]">
                <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
                <span className="text-amber-400/80">步骤 1 描述策略规则 → 步骤 2 预览代码 → 保存</span>
                <div className="ml-auto">
                  <AiProviderSelector entry="strategy_build" value={profileId} onChange={setProfileId} compact />
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2 text-[11px]">
                <Terminal className="h-3.5 w-3.5 text-accent shrink-0" />
                <span className="text-muted">适合有 Python 基础的开发者，手动编写策略文件进行深度定制和二次开发</span>
              </div>
            )}
          </div>

          {/* 内容 */}
          <div className="flex-1 overflow-y-auto px-5 py-5 space-y-4">
            {savedId ? (
              <div className="flex flex-col items-center justify-center py-14 gap-4 text-center">
                <div className="h-12 w-12 rounded-full bg-emerald-400/10 border border-emerald-400/30 flex items-center justify-center">
                  <Check className="h-6 w-6 text-emerald-400" />
                </div>
                <div>
                  <div className="text-sm font-semibold text-foreground">策略已保存</div>
                  <div className="mt-1 font-mono text-xs text-muted">{savedId}</div>
                </div>
                <p className="text-[11px] text-secondary leading-relaxed max-w-sm">
                  已写入策略池，可在选股 / 回测 / 监控中使用。可立即送入回测，或关闭后继续创建下一个策略。
                </p>
                <div className="flex items-center gap-2">
                  <button onClick={sendToBacktest}
                    className="h-9 px-4 rounded-lg bg-accent text-white text-xs font-medium flex items-center gap-1.5 hover:bg-accent/90 transition-all">
                    <Play className="h-3.5 w-3.5" />
                    回测此策略
                  </button>
                  <button onClick={handleClose}
                    className="h-9 px-4 rounded-lg border border-border text-xs text-secondary hover:text-foreground flex items-center gap-1.5 transition-all">
                    关闭
                  </button>
                </div>
              </div>
            ) : tab === 'ai' ? (<>
            {aiStatus && !aiStatus.configured && (
              <div className="rounded-xl border border-amber-400/30 bg-amber-400/5 px-4 py-3 flex items-center gap-3">
                <AlertTriangle className="h-4 w-4 text-amber-400 shrink-0" />
                <div className="flex-1 text-xs text-amber-400/80">AI API Key 未配置，无法生成策略。填写的内容会自动保存。</div>
                <button onClick={() => { persist(); window.location.href = '/settings?tab=ai' }}
                  className="h-7 px-3 rounded-lg bg-amber-400/15 border border-amber-400/30 text-amber-400 text-xs font-medium flex items-center gap-1.5 hover:bg-amber-400/20 shrink-0">
                  <Settings2 className="h-3 w-3" />去配置
                </button>
              </div>
            )}

            {step === 1 ? (
              <>
                <div className="space-y-2">
                  <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder="策略名称，如：强势反包"
                    className="w-full h-9 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm font-medium text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30" />
                  <input type="text" value={description} onChange={e => setDescription(e.target.value)} placeholder="一句话描述，如：筛选前日阴线下跌、今日放量阳线反包的短线强势股"
                    className="w-full h-8 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30" />
                </div>
                <div>
                  <span className="text-[10px] text-muted/50 uppercase tracking-wider mb-1.5 block">选股方向</span>
                  <div className="flex gap-1">
                    {DIRECTIONS.map(d => (
                      <button key={d.value} onClick={() => setDirection(d.value)} className={'px-2.5 py-1 rounded text-[11px] font-medium border transition-colors ' + (direction === d.value ? 'border-amber-400/40 bg-amber-400/10 text-amber-400' : 'border-border bg-base text-muted hover:border-amber-400/30')}>{d.label}</button>
                    ))}
                  </div>
                </div>
                <div>
                  <span className="text-[10px] text-muted/50 uppercase tracking-wider mb-1.5 block">策略规则</span>
                  <textarea value={rules} onChange={e => setRules(e.target.value)}
                    placeholder="描述你的选股逻辑，AI 会自动提取参数。例如：\n前一交易日为明显阴线且跌幅不低于2%，今日阳线收盘反包前一日实体，收盘价接近或高于前一日高点，成交量较前一日放大1.2倍以上，当前 close > ma5 或 close > ma10；使用 filter_history，并优先用 Polars shift/with_columns/filter 实现。"
                    className="w-full h-28 px-3 py-2 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm text-foreground placeholder:text-muted/30 resize-none focus:outline-none focus:ring-2 focus:ring-accent/30" />
                </div>
                {error && <div className="text-[11px] text-danger bg-danger/10 border border-danger/20 rounded-lg px-3 py-2">{error}</div>}
                <button onClick={handleGenerate} disabled={loading || !name.trim() || !rules.trim()}
                  className="w-full h-10 rounded-xl bg-gradient-to-r from-amber-500/20 to-amber-500/10 border border-amber-400/30 text-amber-400 text-sm font-medium flex items-center justify-center gap-2 hover:from-amber-500/30 hover:to-amber-500/20 disabled:opacity-40 transition-all">
                  {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                  {loading ? 'AI 生成中...' : code ? '重新生成' : 'AI 生成策略'}
                </button>
              </>
            ) : (
              <>
                {/* Tab 切换 */}
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-0.5">
                    <button onClick={() => setPreviewTab('params')} className={'px-3 py-1 rounded text-xs font-medium transition-colors ' + (previewTab === 'params' ? 'bg-amber-400/15 text-amber-400' : 'text-muted hover:text-secondary')}>参数</button>
                    <button onClick={() => setPreviewTab('code')} className={'px-3 py-1 rounded text-xs font-medium transition-colors ' + (previewTab === 'code' ? 'bg-amber-400/15 text-amber-400' : 'text-muted hover:text-secondary')}>代码</button>
                  </div>
                </div>

                {previewTab === 'params' ? (
                  hasParams ? (
                    <div className="rounded-xl border border-border/30 bg-surface/30 overflow-hidden max-h-96 overflow-y-auto">
                      <div className="divide-y divide-border/10">
                        {params.length > 0 && (
                          <div className="px-4 py-3 space-y-2">
                            <div className="text-[10px] text-muted/50 uppercase tracking-wider">策略参数</div>
                            {params.map((p: any) => (
                              <div key={p.id} className="flex items-center gap-2">
                                <span className="text-[11px] text-secondary w-24 shrink-0 text-right">{p.label}</span>
                                {p.type === 'bool' ? (
                                  <span className={'text-[11px] font-mono ' + (p.default ? 'text-accent' : 'text-muted')}>{p.default ? '✓ 是' : '✗ 否'}</span>
                                ) : (
                                  <>
                                    <span className="text-[11px] font-mono text-foreground">{p.default}</span>
                                    <span className="text-[10px] text-muted">{p.min} ~ {p.max}{p.type === 'float' || p.type === 'int' ? ' · 步长 ' + p.step : ''}</span>
                                  </>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                        {(entrySignals.length > 0 || exitSignals.length > 0) && (
                          <div className="px-4 py-3 space-y-2">
                            <div className="text-[10px] text-muted/50 uppercase tracking-wider">交易信号</div>
                            {entrySignals.length > 0 && (
                              <div className="flex items-center gap-2">
                                <span className="text-[10px] text-emerald-400 w-10 shrink-0">买入</span>
                                <div className="flex flex-wrap gap-0.5">
                                  {entrySignals.map((s: string) => <span key={s} className="px-1.5 py-0.5 rounded bg-emerald-400/10 text-emerald-400 text-[10px] font-mono">{s}</span>)}
                                </div>
                              </div>
                            )}
                            {exitSignals.length > 0 && (
                              <div className="flex items-center gap-2">
                                <span className="text-[10px] text-danger w-10 shrink-0">卖出</span>
                                <div className="flex flex-wrap gap-0.5">
                                  {exitSignals.map((s: string) => <span key={s} className="px-1.5 py-0.5 rounded bg-danger/10 text-danger text-[10px] font-mono">{s}</span>)}
                                </div>
                              </div>
                            )}
                            <div className="flex items-center gap-4 text-[10px] text-muted mt-1">
                              {codeStopLoss !== null && <span>止损: {(codeStopLoss * 100).toFixed(1)}%</span>}
                              {codeHoldDays !== null && <span>持有: {codeHoldDays} 天</span>}
                            </div>
                          </div>
                        )}
                        {Object.keys(scoring).length > 0 && (
                          <div className="px-4 py-3 space-y-2">
                            <div className="text-[10px] text-muted/50 uppercase tracking-wider">评分权重</div>
                            {Object.entries(scoring).map(([k, v]) => (
                              <div key={k} className="flex items-center gap-2">
                                <span className="text-[10px] text-muted w-24 shrink-0 text-right font-mono">{k}</span>
                                <div className="flex-1 h-1.5 bg-elevated rounded-full overflow-hidden">
                                  <div className="h-full bg-amber-400/60 rounded-full" style={{ width: Math.min(v * 100, 100) + '%' }} />
                                </div>
                                <span className="w-8 text-right text-[10px] font-mono text-muted">{Math.round(v * 100)}%</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="rounded-xl border border-border/30 bg-surface/30 px-4 py-6 text-[11px] text-muted text-center">未检测到策略参数，切换「代码」查看完整内容</div>
                  )
                ) : (
                  <pre className="bg-base border border-border/30 rounded-lg p-3 text-[11px] font-mono text-foreground/80 overflow-auto max-h-96 whitespace-pre-wrap">{code}</pre>
                )}

                {error && <div className="text-[11px] text-danger bg-danger/10 border border-danger/20 rounded-lg px-3 py-2">{error}</div>}

                <div className="flex gap-2">
                  <input type="text" value={instruction} onChange={e => setInstruction(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && handleModify()}
                    placeholder="调整策略逻辑，如：增加RSI超卖条件、要求今日放量、修改均线为30日..."
                    className="flex-1 h-9 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30" />
                  <button onClick={handleModify} disabled={loading || !instruction.trim()}
                    className="h-9 px-4 rounded-lg bg-amber-400/15 border border-amber-400/30 text-amber-400 text-xs font-medium flex items-center gap-1.5 hover:bg-amber-400/20 disabled:opacity-40 transition-all">
                    {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                    AI 修改
                  </button>
                </div>
                <p className="text-[10px] text-muted/40">预览尚未写入策略池。修改指令可调整参数、信号、告警、评分。确认无误后点击「保存策略」，才会出现在选股 / 回测 / 监控。</p>
              </>
            )}
            </>
            ) : (
              /* 自定义编写 */
              <div className="space-y-4">
                <div className="rounded-xl border border-border/40 bg-elevated/50 p-4 space-y-2.5">
                  <div className="flex items-center gap-2">
                    <Terminal className="h-4 w-4 text-accent" />
                    <span className="text-sm font-medium text-foreground">自定义策略开发方式</span>
                  </div>
                  <div className="space-y-1.5 text-[11px] text-secondary leading-relaxed">
                    <p>在项目目录 <code className="px-1 py-0.5 rounded bg-base text-xs font-mono text-foreground/80">data/strategies/custom/</code> 下创建 <code className="px-1 py-0.5 rounded bg-base text-xs font-mono text-foreground/80">.py</code> 文件。支持两种模式：</p>
                    <div className="space-y-1 pl-1">
                      <div className="flex items-start gap-1.5">
                        <span className="mt-0.5 h-1.5 w-1.5 rounded-full bg-accent/60 shrink-0" />
                        <span><strong className="text-foreground/80">模式 A：单日过滤</strong> — <code className="text-[10px] font-mono text-foreground/80">filter(df, params) → pl.Expr</code></span>
                      </div>
                      <div className="flex items-start gap-1.5">
                        <span className="mt-0.5 h-1.5 w-1.5 rounded-full bg-amber-400/60 shrink-0" />
                        <span><strong className="text-foreground/80">模式 B：历史窗口</strong> — <code className="text-[10px] font-mono text-foreground/80">filter_history(df, params) → pl.DataFrame</code> + <code className="text-[10px] font-mono text-foreground/80">LOOKBACK_DAYS</code></span>
                      </div>
                    </div>
                    <p>完整规范见 <span className="text-accent">docs/strategy-guide.md</span></p>
                  </div>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-foreground">快速模板</span>
                    <button onClick={() => { navigator.clipboard.writeText(CUSTOM_TEMPLATE); setCustomCopied(true); setTimeout(() => setCustomCopied(false), 2000) }}
                      className={cn('inline-flex items-center gap-1 px-2 py-1 rounded text-[10px] font-medium transition-all cursor-pointer', customCopied ? 'bg-emerald-400/10 text-emerald-400' : 'bg-elevated text-muted hover:text-foreground hover:bg-accent/10')}>
                      {customCopied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                      {customCopied ? '已复制' : '复制模板'}
                    </button>
                  </div>
                  <pre className="rounded-xl border border-border/40 bg-base p-4 text-[10px] leading-relaxed font-mono text-foreground/70 overflow-auto max-h-[400px]">{CUSTOM_TEMPLATE}</pre>
                </div>
              </div>
            )}
          </div>

          {/* 底部 */}
          {tab === 'ai' && !savedId && (
          <div className="flex items-center justify-between px-5 py-3 border-t border-border/50 bg-surface/50">
            <button onClick={clearDraft} className="text-[10px] text-muted/40 hover:text-danger transition-colors">重新创建</button>
            <div className="flex items-center gap-2">
              {step === 1 && code && name.trim() && (
                <button onClick={() => setStep(2)} className="h-7 px-3 rounded-lg border border-border text-xs text-secondary hover:text-foreground flex items-center gap-1">
                  前往下一步 <ChevronRight className="h-3 w-3" />
                </button>
              )}
              {step === 2 && (
                <>
                  <button onClick={() => setStep(1)} className="h-7 px-3 rounded-lg border border-border text-xs text-secondary hover:text-foreground flex items-center gap-1">
                    <ChevronLeft className="h-3 w-3" />上一步
                  </button>
                  <button onClick={handleSave} disabled={saving || loading}
                    className="inline-flex items-center gap-1.5 h-7 px-3 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent/90 disabled:opacity-50 transition-all">
                    <Save className="h-3 w-3" />
                    {saving ? '保存中...' : '保存策略'}
                  </button>
                </>
              )}
            </div>
          </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
