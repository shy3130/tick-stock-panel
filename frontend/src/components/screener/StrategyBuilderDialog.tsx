import { useState, useEffect, useCallback } from 'react'
import { Modal } from '@/components/Modal'
import { X, Sparkles, Save, Loader2, ChevronLeft, ChevronRight, AlertTriangle, Settings2, FileText, Copy, Check, Terminal } from 'lucide-react'
import { api } from '@/lib/api'
import { storage } from '@/lib/storage'
import { cn } from '@/lib/cn'

// ===== 工具函数 =====

function parsePyValue(v: string): any {
  const s = v.trim()
  if (s === 'True') return true
  if (s === 'False') return false
  if (s === 'None') return null
  return JSON.parse(s)
}

function slugId(prefix: 'ai' | 'custom' = 'ai'): string {
  return prefix + '_' + Date.now().toString(36)
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
  const [step, setStep] = useState(1)
  const [tab, setTab] = useState<'ai' | 'custom'>('ai')
  const [customCopied, setCustomCopied] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [direction, setDirection] = useState('long')
  const [rules, setRules] = useState('')
  const [code, setCode] = useState('')
  const [instruction, setInstruction] = useState('')
  const [previewTab, setPreviewTab] = useState<'params' | 'code'>('params')
  const [strategyId, setStrategyId] = useState('')
  const [source, setSource] = useState<'ai' | 'custom'>('ai')
  const [validated, setValidated] = useState(false)

  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [aiStatus, setAiStatus] = useState<{ configured: boolean } | null>(null)
  const [checkedAi, setCheckedAi] = useState(false)
  const [loaded, setLoaded] = useState(false)

  // 打开时恢复草稿
  useEffect(() => {
    if (!open) { setLoaded(false); return }
    const d = draftStore.get(null)
    if (d) {
      setStep(d.step ?? 1); setName(d.name ?? ''); setDescription(d.description ?? '')
      setDirection(d.direction ?? 'long')
      setRules(d.rules ?? ''); setCode(d.code ?? ''); setStrategyId(d.strategyId ?? '')
      setSource((d as any).source ?? (d.strategyId?.startsWith('custom_') ? 'custom' : 'ai'))
      if (mode === 'modify') setTab('custom')
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
      draftStore.set({ name, description, direction, rules, code, step, strategyId, source } as any)
    }
  }, [name, description, direction, rules, code, step, strategyId, source])
  useEffect(() => { if (loaded) persist() }, [loaded, persist])

  const clearDraft = () => {
    setName(''); setDescription(''); setDirection('long')
    setRules(''); setCode(''); setStep(1); setError(''); setInstruction('')
    setStrategyId(''); setSource('ai'); setValidated(false)
  }

  const handleClose = () => { if (name || rules || code) persist(); onClose() }

  const resolveStrategyId = (target: 'ai' | 'custom' = source) => {
    if (mode === 'modify' && strategyId) return strategyId
    if (strategyId && strategyId.startsWith(target + '_')) return strategyId
    return slugId(target)
  }

  // Step 1: 生成
  const handleGenerate = async () => {
    if (!name.trim() || !rules.trim()) return
    if (!aiStatus?.configured) { setError('AI 未配置，请在设置页面配置 API Key'); return }
    setLoading(true); setError(''); setCode(''); setValidated(false)
    try {
      const id = resolveStrategyId('ai')
      setStrategyId(id); setSource('ai'); setPreviewTab('code')
      let finalResult: any = null
      for await (const evt of api.strategyBuildStream(1, { name: name.trim(), description: description.trim(), direction, rules: rules.trim(), strategy_id: id })) {
        if (evt.type === 'delta') {
          setCode(prev => prev + evt.content)
        } else if (evt.type === 'error') {
          throw new Error(evt.message)
        } else if (evt.type === 'result') {
          finalResult = evt
        }
      }
      if (!finalResult) throw new Error('AI 未返回策略结果')
      if (!finalResult.valid) { setError(finalResult.error ?? '生成失败'); return }
      setCode(finalResult.code); setStep(2); setValidated(true)
      const genDesc = parseMetaField(finalResult.code, 'description')
      const genRules = parseRules(finalResult.code)
      if (genDesc) setDescription(genDesc)
      if (genRules) setRules(genRules)
    } catch (e: any) {
      const msg = String(e?.message ?? '')
      setError(msg.includes('API Key') || msg.includes('api_key') ? 'AI API Key 未配置或无效' : (msg || '生成失败'))
    } finally { setLoading(false) }
  }

  // Step 2: AI 修改
  const handleModify = async () => {
    if (!instruction.trim() || !code) return
    setLoading(true); setError(''); setValidated(false)
    try {
      let draft = ''
      let finalResult: any = null
      for await (const evt of api.strategyBuildStream(2, { current_code: code, instruction: instruction.trim(), strategy_id: strategyId })) {
        if (evt.type === 'delta') {
          draft += evt.content
          setCode(draft)
        } else if (evt.type === 'error') {
          throw new Error(evt.message)
        } else if (evt.type === 'result') {
          finalResult = evt
        }
      }
      if (!finalResult) throw new Error('AI 未返回策略结果')
      if (!finalResult.valid) { setError(finalResult.error ?? '修改失败'); return }
      setCode(finalResult.code); setInstruction(''); setValidated(true)
      const genDesc = parseMetaField(finalResult.code, 'description')
      const updatedRules = parseRules(finalResult.code)
      if (genDesc) setDescription(genDesc)
      if (updatedRules) setRules(updatedRules)
    } catch (e: any) { setError(String(e?.message ?? '修改失败')) }
    finally { setLoading(false) }
  }

  const handleValidateCode = async () => {
    const draftCode = code
    if (!draftCode.trim()) return
    setLoading(true); setError('')
    try {
      const id = strategyId || resolveStrategyId(tab === 'custom' ? 'custom' : 'ai')
      setStrategyId(id)
      const res = await api.strategyValidateCode({ code: draftCode, strategy_id: id, name: name.trim(), description: description.trim(), strict: true })
      if (!res.valid) { setValidated(false); setError(res.error ?? '代码校验失败'); return }
      setCode(res.code); setValidated(true)
      const genDesc = parseMetaField(res.code, 'description')
      const genRules = parseRules(res.code)
      if (genDesc) setDescription(genDesc)
      if (genRules) setRules(genRules)
    } catch (e: any) { setError(String(e?.message ?? '代码校验失败')) }
    finally { setLoading(false) }
  }

  // 保存
  const handleSave = async () => {
    const draftCode = code
    if (!draftCode) return
    setSaving(true); setError('')
    try {
      const target = mode === 'modify' ? source : (tab === 'custom' ? 'custom' : 'ai')
      const id = resolveStrategyId(target)
      setStrategyId(id); setSource(target)
      await api.strategySaveCodeV2({
        strategy_id: id,
        code: draftCode,
        target_source: target,
        mode: mode === 'modify' ? 'update' : 'create',
        name: name.trim(),
        description: description.trim(),
        strict: true,
      })
      const genRules = parseRules(draftCode)
      const finalRules = (genRules || rules).trim()
      if (finalRules) { const saved = storage.strategyRules.get({}); saved[id] = finalRules; storage.strategyRules.set(saved) }
      await onSavedId?.(id)
      clearDraft()
      setTimeout(() => onClose(), 1000)
    } catch (e: any) { setError(String(e?.message ?? '保存失败')) }
    setSaving(false)
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
    <Modal
      onClose={handleClose}
      labelledBy="strategy-builder-title"
      overlayClassName="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      panelClassName="w-[820px] max-h-[88vh] bg-surface/95 backdrop-blur-xl border border-border/50 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
    >

          {/* 标题 */}
          <div className="grid grid-cols-[1fr_auto_1fr] items-center px-5 py-2.5 border-b border-border/50">
            {/* 左侧：Tab 切换 */}
            <div className="flex rounded-lg bg-elevated p-0.5 w-fit">
              <button onClick={() => { setTab('ai'); if (mode === 'create') setSource('ai') }} className={cn('px-3 py-1 rounded-md text-xs font-medium transition-all cursor-pointer', tab === 'ai' ? 'bg-amber-400/15 text-amber-400' : 'text-muted hover:text-foreground')}>
                <Sparkles className="h-3 w-3 inline mr-1" />AI 生成
              </button>
              <button onClick={() => { setTab('custom'); if (mode === 'create') { setSource('custom'); if (!code) setCode(CUSTOM_TEMPLATE) } }} className={cn('px-3 py-1 rounded-md text-xs font-medium transition-all cursor-pointer', tab === 'custom' ? 'bg-accent/15 text-accent' : 'text-muted hover:text-foreground')}>
                <FileText className="h-3 w-3 inline mr-1" />自定义编写
              </button>
            </div>
            {/* 中间：标题 */}
            <span id="strategy-builder-title" className="text-sm font-semibold text-foreground">
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
              <button aria-label="关闭" onClick={handleClose} className="p-1.5 rounded-lg hover:bg-elevated"><X className="h-4 w-4 text-muted" /></button>
            </div>
          </div>

          {/* Tab 描述 */}
          <div className="px-5 py-2 border-b border-border/30 bg-elevated/30">
            {tab === 'ai' ? (
              <div className="flex items-center gap-2 text-[11px]">
                <Sparkles className="h-3.5 w-3.5 text-amber-400 shrink-0" />
                <span className="text-amber-400/80">步骤 1 描述策略规则 → 步骤 2 预览代码 → 保存</span>
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
            {tab === 'ai' ? (<>
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
                <p className="text-[10px] text-muted/40">修改指令可调整参数、信号、告警、评分等任意内容。确认无误后点击「保存策略」。</p>
              </>
            )}
            </>
            ) : (
              /* 自定义编写 */
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-2">
                  <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder="策略名称，如：我的反包策略"
                    className="h-9 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm font-medium text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30" />
                  <input type="text" value={description} onChange={e => setDescription(e.target.value)} placeholder="一句话描述策略逻辑"
                    className="h-9 px-3 rounded-lg bg-base border-0 ring-1 ring-border/30 text-sm text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30" />
                </div>
                <div className="rounded-xl border border-border/40 bg-elevated/50 p-4 space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Terminal className="h-4 w-4 text-accent" />
                      <span className="text-sm font-medium text-foreground">自定义策略代码</span>
                      {validated && <span className="text-[10px] text-emerald-400">已校验</span>}
                    </div>
                    <button onClick={() => { navigator.clipboard.writeText(code || CUSTOM_TEMPLATE); setCustomCopied(true); setTimeout(() => setCustomCopied(false), 2000) }}
                      className={cn('inline-flex items-center gap-1 px-2 py-1 rounded text-[10px] font-medium transition-all cursor-pointer', customCopied ? 'bg-emerald-400/10 text-emerald-400' : 'bg-elevated text-muted hover:text-foreground hover:bg-accent/10')}>
                      {customCopied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                      {customCopied ? '已复制' : '复制代码'}
                    </button>
                  </div>
                  <textarea
                    value={code}
                    onChange={e => { setCode(e.target.value); setValidated(false) }}
                    spellCheck={false}
                    className="w-full h-[420px] rounded-xl border border-border/40 bg-base p-4 text-[11px] leading-relaxed font-mono text-foreground/80 resize-none focus:outline-none focus:ring-2 focus:ring-accent/30"
                  />
                  <div className="text-[11px] text-muted leading-relaxed">
                    新建自定义策略会保存到 <code className="px-1 py-0.5 rounded bg-base text-xs font-mono text-foreground/80">data/strategies/custom/</code>；修改已有策略会保存回原文件。
                  </div>
                  {error && <div className="text-[11px] text-danger bg-danger/10 border border-danger/20 rounded-lg px-3 py-2">{error}</div>}
                  <div className="flex items-center justify-end gap-2">
                    <button onClick={() => { setCode(CUSTOM_TEMPLATE); setStrategyId(''); setSource('custom'); setValidated(false) }}
                      className="h-8 px-3 rounded-lg border border-border text-xs text-secondary hover:text-foreground">
                      使用模板
                    </button>
                    <button onClick={handleValidateCode} disabled={loading || !code.trim()}
                      className="h-8 px-3 rounded-lg border border-accent/30 bg-accent/10 text-accent text-xs font-medium hover:bg-accent/15 disabled:opacity-40 flex items-center gap-1.5">
                      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                      校验代码
                    </button>
                    <button onClick={handleSave} disabled={saving || loading || !code.trim()}
                      className="h-8 px-3 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent/90 disabled:opacity-50 flex items-center gap-1.5">
                      <Save className="h-3.5 w-3.5" />
                      {saving ? '保存中...' : mode === 'modify' ? '保存修改' : '保存自定义策略'}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* 底部 */}
          {tab === 'ai' && (
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
    </Modal>
  )
}
