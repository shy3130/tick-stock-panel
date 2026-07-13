import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Save, X, Plus, Search } from 'lucide-react'
import { api, genRuleId, type MonitorRule, type MonitorCondition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { SignalPicker } from '@/components/screener/SignalPicker'
import { usePreferences } from '@/lib/useSharedQueries'

interface Props {
  /** 编辑现有规则;null=新建 */
  rule: MonitorRule | null
  /** 新建时的预填值 (如个股弹窗传入 symbol/scope) */
  preset?: Partial<MonitorRule>
  /** 极简模式: 个股场景, 隐藏 type/scope/阈值等, 只显示信号点选 */
  simple?: boolean
  onClose: () => void
  onSaved?: () => void
}

const TYPE_DEFAULT_NAME: Record<string, string> = {
  signal: '个股信号监控', price: '价格监控', market: '市场异动监控', strategy: '策略监控',
}

const emptyRule = (preset?: Partial<MonitorRule>): MonitorRule => ({
  id: genRuleId(),
  name: '',
  enabled: true,
  type: 'signal',
  asset_type: 'stock',
  scope: 'symbols',
  symbols: [],
  sector: null,
  strategy_id: null,
  direction: 'entry',
  conditions: [],
  logic: 'or',
  cooldown_seconds: 3600,
  severity: 'info',
  message: '',
  ...preset,
})

export function RuleEditor({ rule, preset, simple, onClose, onSaved }: Props) {
  const qc = useQueryClient()
  const options = useQuery({ queryKey: QK.monitorRuleOptions, queryFn: api.monitorRuleOptions })
  const { data: prefs } = usePreferences()
  const feishuConfigured = !!(prefs?.feishu_webhook_url)
  const wecomConfigured = !!(prefs?.wecom_webhook_url)
  const [editing] = useState(!!rule)
  // 新建规则: 预填全局「默认推送渠道」(多选数组), preset 显式指定时以 preset 为准。
  // 编辑规则: 完全沿用规则自身配置, 不受默认值影响。
  const [draft, setDraft] = useState<MonitorRule>(
    rule
      ? { ...rule, conditions: rule.conditions.map(c => ({ ...c })) }
      : {
          ...emptyRule(preset),
          webhook_channels: preset?.webhook_channels ?? (prefs?.webhook_default_channels ?? []),
        },
  )
  const assetType = draft.asset_type ?? 'stock'
  // 策略列表跟随资产类型: ETF 只列技术类策略。
  const strategies = useQuery({
    queryKey: QK.screenerStrategies(assetType),
    queryFn: () => api.screenerStrategies(assetType),
  })
  const [error, setError] = useState('')
  const [symbolQuery, setSymbolQuery] = useState('')
  // ETF 规则时标的搜索一并搜出 ETF。
  const symbolAssetTypes = assetType === 'etf' ? 'stock,etf' : 'stock'
  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbolQuery, symbolAssetTypes),
    queryFn: () => api.instrumentSearch(symbolQuery, 20, symbolAssetTypes),
    enabled: symbolQuery.length > 0,
  })

  const save = useMutation({
    mutationFn: () => {
      const d = { ...draft }
      // name 为空时用默认名
      if (!d.name.trim()) {
        const base = TYPE_DEFAULT_NAME[d.type] ?? '监控规则'
        d.name = d.scope === 'symbols' && d.symbols.length > 0
          ? `${base} · ${d.symbols[0]}${d.symbols.length > 1 ? ` 等${d.symbols.length}只` : ''}`
          : base
      }
      if (d.type === 'strategy') {
        if (!d.strategy_id) throw new Error('策略监控必须选择一个策略')
      } else {
        if (d.conditions.length === 0) throw new Error('至少选择一个触发条件')
        for (const c of d.conditions) {
          if (!c.field || !c.op) throw new Error('条件填写不完整')
          if (c.op !== 'truth' && (c.value === null || c.value === undefined)) throw new Error('阈值条件需要数值')
        }
      }
      if (d.scope === 'symbols' && d.symbols.length === 0) throw new Error('请选择至少一只股票')
      return api.monitorRuleSave(d)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      onSaved?.()
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  // 条件编辑
  const updateCond = (idx: number, patch: Partial<MonitorCondition>) =>
    setDraft(d => ({ ...d, conditions: d.conditions.map((c, i) => i === idx ? { ...c, ...patch } : c) }))
  const addCond = (op: 'truth' | 'threshold') =>
    setDraft(d => ({
      ...d,
      conditions: [...d.conditions, op === 'truth'
        ? { field: 'signal_volume_surge', op: 'truth' }
        // simple 模式(个股弹窗)默认现价; 完整模式默认 RSI 超卖
        : { field: simple ? 'close' : 'rsi_14', op: '<', value: simple ? 0 : 30 }],
    }))
  const removeCond = (idx: number) =>
    setDraft(d => ({ ...d, conditions: d.conditions.filter((_, i) => i !== idx) }))

  const addSymbol = (sym: string) => {
    if (!draft.symbols.includes(sym)) {
      setDraft(d => ({ ...d, symbols: [...d.symbols, sym] }))
    }
    setSymbolQuery('')
  }

  // 勾选/取消勾选某个推送渠道 (飞书 / 企业微信 各自独立)
  const toggleChannel = (ch: string) =>
    setDraft(d => {
      const cur = d.webhook_channels ?? []
      return { ...d, webhook_channels: cur.includes(ch) ? cur.filter(c => c !== ch) : [...cur, ch] }
    })

  const thresholdFields = options.data?.threshold_fields ?? []
  const operators = options.data?.operators ?? ['>', '>=', '<', '<=', '==', '!=']
  const selectedSignals = draft.conditions.filter(c => c.op === 'truth').map(c => c.field)
  const thresholdConds = draft.conditions.filter(c => c.op !== 'truth')

  const onSignalPickerChange = (next: string[]) => {
    const nonTruthConds = draft.conditions.filter(c => c.op !== 'truth')
    const truthConds: MonitorCondition[] = next.map(field => ({ field, op: 'truth' }))
    setDraft(d => ({ ...d, conditions: [...nonTruthConds, ...truthConds] }))
  }

  // ── 极简模式: 只显示信号点选 + 可选描述 ──
  if (simple) {
    return (
      <div className="rounded-card border border-border bg-surface p-5 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-medium text-foreground">{editing ? '编辑监控' : '加入监控'}</h3>
          <button onClick={onClose} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground cursor-pointer">
            <X className="h-4 w-4" />
          </button>
        </div>

        {draft.symbols.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {draft.symbols.map(s => (
              <span key={s} className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-secondary font-mono">{s}</span>
            ))}
          </div>
        )}

        <div>
          <div className="mb-1.5 text-[11px] text-muted">选择触发信号 (任一命中即报警)</div>
          <SignalPicker signals={selectedSignals} onChange={onSignalPickerChange} kind="entry" />
        </div>

        {/* 价位条件 (阈值) — 与信号共存, 可选添加 */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-muted">价位条件 (可选)</span>
            <button onClick={() => addCond('threshold')} className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer">
              <Plus className="h-3 w-3" />添加价位
            </button>
          </div>
          {thresholdConds.length > 0 && (
            <div className="space-y-1.5">
              {thresholdConds.map((c, i) => {
                const realIdx = draft.conditions.indexOf(c)
                return (
                  <div key={i} className="flex items-center gap-1.5">
                    <span className="text-[10px] text-muted/60 w-6 text-right shrink-0">{i === 0 && selectedSignals.length === 0 ? '当' : draft.logic === 'and' ? '且' : '或'}</span>
                    <select value={c.field} onChange={e => updateCond(realIdx, { field: e.target.value })} className="flex-1 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50">
                      {thresholdFields.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
                    </select>
                    <select value={c.op} onChange={e => updateCond(realIdx, { op: e.target.value })} className="w-12 h-7 px-1 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                      {operators.map(op => <option key={op} value={op}>{op}</option>)}
                    </select>
                    <input type="number" value={c.value ?? 0} onChange={e => updateCond(realIdx, { value: parseFloat(e.target.value) })} step="any" className="w-24 h-7 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                    <button onClick={() => removeCond(realIdx)} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <label className="space-y-1.5">
          <span className="text-[11px] text-muted">备注 (可选)</span>
          <input value={draft.message} onChange={e => setDraft(d => ({ ...d, message: e.target.value }))} placeholder="给这条监控加个备注" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
        </label>

        {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs cursor-pointer">取消</button>
          <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50 cursor-pointer">
            <Save className="h-3.5 w-3.5" />加入监控
          </button>
        </div>
      </div>
    )
  }

  // ── 完整模式: 监控页新建/编辑 ──
  return (
    <div className="rounded-card border border-border bg-surface p-5 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium text-foreground">{editing ? '编辑监控规则' : '新建监控规则'}</h3>
          <p className="mt-1 text-[11px] text-muted">规则标识自动生成,描述为可选。</p>
        </div>
        <button onClick={onClose} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground cursor-pointer">
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* 资产类型: 股票 / ETF (个股极简模式不显示) */}
      {!simple && (
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted">资产类型</span>
          <div className="inline-flex h-9 rounded-btn border border-border overflow-hidden">
            {(['stock', 'etf'] as const).map(t => (
              <button
                key={t}
                type="button"
                onClick={() => setDraft(d => ({ ...d, asset_type: t, strategy_id: null, symbols: [] }))}
                className={`h-full px-4 text-xs font-medium transition-colors cursor-pointer
                  ${assetType === t ? 'bg-accent/10 text-accent' : 'text-muted hover:text-foreground'}`}
              >
                {t === 'stock' ? '股票' : 'ETF'}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 描述 (可选) + 类型 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <label className="md:col-span-2 space-y-1.5">
          <span className="text-[11px] text-muted">描述 (可选)</span>
          <input value={draft.name} onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} placeholder="留空用默认名称" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] text-muted">监控类型</span>
          <select value={draft.type} onChange={e => setDraft(d => ({ ...d, type: e.target.value as MonitorRule['type'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
            {(options.data?.types ?? []).map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
          </select>
        </label>
      </div>

      {/* 作用范围 */}
      <div className="space-y-2">
        <span className="text-[11px] text-muted">作用范围</span>
        <div className="flex items-center gap-2">
          <select value={draft.scope} onChange={e => setDraft(d => ({ ...d, scope: e.target.value as MonitorRule['scope'] }))} className="h-9 w-32 rounded-btn border border-border bg-base px-3 text-xs text-foreground">
            {(options.data?.scopes ?? []).map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
          {draft.scope === 'symbols' && (
            <div className="flex-1 flex flex-wrap items-center gap-1.5">
              {draft.symbols.map(sym => (
                <span key={sym} className="inline-flex items-center gap-1 rounded bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">
                  {sym}
                  <button onClick={() => setDraft(d => ({ ...d, symbols: d.symbols.filter(s => s !== sym) }))} className="text-muted hover:text-danger cursor-pointer">
                    <X className="h-2.5 w-2.5" />
                  </button>
                </span>
              ))}
              <div className="relative">
                <input
                  value={symbolQuery}
                  onChange={e => setSymbolQuery(e.target.value)}
                  placeholder="搜索股票..."
                  className="h-7 w-32 rounded border border-border bg-base pl-6 pr-2 text-[11px] text-foreground focus:outline-none focus:border-accent/50"
                />
                <Search className="absolute left-1.5 top-1.5 h-3.5 w-3.5 text-muted" />
                {symbolSearch.data && symbolSearch.data.results.length > 0 && (
                  <div className="absolute z-10 mt-1 max-h-48 w-48 overflow-auto rounded border border-border bg-surface shadow-lg">
                    {symbolSearch.data.results.map(r => (
                      <button key={r.symbol} onClick={() => addSymbol(r.symbol)} className="block w-full px-2 py-1 text-left text-[11px] hover:bg-elevated cursor-pointer">
                        <span className="font-mono text-foreground/80">{r.symbol}</span>
                        <span className="ml-1 text-muted">{r.name}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
          {draft.scope === 'all' && <span className="text-[11px] text-muted">对全市场所有股票生效</span>}
          {draft.scope === 'sector' && <span className="text-[11px] text-muted/60">板块精确过滤(开发中,当前等同全市场)</span>}
        </div>
      </div>

      {/* 触发条件 (非 strategy) */}
      {draft.type !== 'strategy' && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-muted">触发条件</span>
            <div className="flex items-center gap-2">
              <select value={draft.logic} onChange={e => setDraft(d => ({ ...d, logic: e.target.value as MonitorRule['logic'] }))} className="h-7 rounded border border-border bg-base px-1.5 text-[11px] text-foreground">
                {(options.data?.logics ?? []).map(l => <option key={l.key} value={l.key}>{l.label}</option>)}
              </select>
              <button onClick={() => addCond('truth')} className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer">
                <Plus className="h-3 w-3" />信号条件
              </button>
              <button onClick={() => addCond('threshold')} className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer">
                <Plus className="h-3 w-3" />阈值条件
              </button>
            </div>
          </div>

          {selectedSignals.length > 0 || (options.data?.builtin_signals ?? []).length > 0 ? (
            <div>
              <div className="mb-1.5 text-[10px] text-muted/70">信号条件 (点选)</div>
              <SignalPicker signals={selectedSignals} onChange={onSignalPickerChange} kind="entry" />
            </div>
          ) : null}

          {thresholdConds.length > 0 && (
            <div className="space-y-1.5">
              {thresholdConds.map((c, i) => {
                const realIdx = draft.conditions.indexOf(c)
                return (
                  <div key={i} className="flex items-center gap-1.5">
                    <span className="text-[10px] text-muted/60 w-6 text-right shrink-0">{i === 0 && selectedSignals.length === 0 ? '当' : draft.logic === 'and' ? '且' : '或'}</span>
                    <select value={c.field} onChange={e => updateCond(realIdx, { field: e.target.value })} className="w-32 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50">
                      {thresholdFields.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
                    </select>
                    <select value={c.op} onChange={e => updateCond(realIdx, { op: e.target.value })} className="w-12 h-7 px-1 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                      {operators.map(op => <option key={op} value={op}>{op}</option>)}
                    </select>
                    <input type="number" value={c.value ?? 0} onChange={e => updateCond(realIdx, { value: parseFloat(e.target.value) })} step="any" className="w-24 h-7 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
                    <button onClick={() => removeCond(realIdx)} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                )
              })}
            </div>
          )}

          {draft.conditions.length === 0 && (
            <div className="rounded border border-dashed border-border px-3 py-4 text-center text-[11px] text-muted">
              点击上方「信号条件」或「阈值条件」添加触发规则
            </div>
          )}
        </div>
      )}

      {/* strategy 类型: 选策略 + 方向 */}
      {draft.type === 'strategy' && (
        <div className="space-y-2">
          <span className="text-[11px] text-muted">策略与方向</span>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <label className="md:col-span-2 space-y-1.5">
              <span className="text-[10px] text-muted/70">选择策略</span>
              <select
                value={draft.strategy_id ?? ''}
                onChange={e => setDraft(d => ({ ...d, strategy_id: e.target.value || null }))}
                className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground"
              >
                <option value="">— 请选择 —</option>
                {(strategies.data?.presets ?? []).map(s => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </label>
            <label className="space-y-1.5">
              <span className="text-[10px] text-muted/70">触发方向</span>
              <select
                value={draft.direction}
                onChange={e => setDraft(d => ({ ...d, direction: e.target.value as MonitorRule['direction'] }))}
                className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground"
              >
                {(options.data?.directions ?? []).map(d => <option key={d.key} value={d.key}>{d.label}</option>)}
              </select>
            </label>
          </div>
          <p className="text-[10px] leading-4 text-muted/70">
            策略监控自动评估策略的出入场信号。entry=入场信号,exit=出场信号,both=两者都报。作用范围建议用「全市场」。
          </p>
        </div>
      )}

      {/* 通知设置 */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <label className="space-y-1.5">
          <span className="text-[11px] text-muted">冷却期(秒)</span>
          <input type="number" value={draft.cooldown_seconds} onChange={e => setDraft(d => ({ ...d, cooldown_seconds: parseInt(e.target.value) || 0 }))} min={0} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] text-muted">严重级别</span>
          <select value={draft.severity} onChange={e => setDraft(d => ({ ...d, severity: e.target.value as MonitorRule['severity'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
            {(options.data?.severities ?? []).map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
        <label className="space-y-1.5 md:col-span-1">
          <span className="text-[11px] text-muted">自定义提示(可选)</span>
          <input value={draft.message} onChange={e => setDraft(d => ({ ...d, message: e.target.value }))} placeholder="留空用默认文案" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
        </label>
      </div>

      {/* Webhook 推送 — 飞书 / 企业微信 */}
      <div className="rounded-btn border border-border/40 bg-base/40 p-3 space-y-2">
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] font-medium text-foreground">Webhook 推送</span>
          <span className="text-[9px] text-muted">触发时推送告警到外部</span>
        </div>

        {/* 渠道列表 */}
        <div className="space-y-1.5">
          {/* 飞书 (可用) */}
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={(draft.webhook_channels ?? []).includes('feishu')}
              onChange={() => toggleChannel('feishu')}
              className="h-3 w-3 accent-accent cursor-pointer"
            />
            <span className="text-[11px] text-foreground">飞书</span>
            <span className="text-[9px] text-muted">群推送 Webhook</span>
            {(draft.webhook_channels ?? []).includes('feishu') && (
              <span className={`ml-auto text-[9px] ${feishuConfigured ? 'text-emerald-500' : 'text-warning'}`}>
                {feishuConfigured ? '已配置' : '未配置'}
              </span>
            )}
          </label>

          {/* 企业微信 (可用) */}
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={(draft.webhook_channels ?? []).includes('wecom')}
              onChange={() => toggleChannel('wecom')}
              className="h-3 w-3 accent-accent cursor-pointer"
            />
            <span className="text-[11px] text-foreground">企业微信</span>
            <span className="text-[9px] text-muted">群推送 Webhook</span>
            {(draft.webhook_channels ?? []).includes('wecom') && (
              <span className={`ml-auto text-[9px] ${wecomConfigured ? 'text-emerald-500' : 'text-warning'}`}>
                {wecomConfigured ? '已配置' : '未配置'}
              </span>
            )}
          </label>

        </div>

        {/* 勾选了某渠道但该渠道地址未配置 → 提示前往设置 */}
        {(draft.webhook_channels ?? []).length > 0 && (() => {
          const selected = draft.webhook_channels ?? []
          const unconfigured: string[] = []
          if (selected.includes('feishu') && !feishuConfigured) unconfigured.push('飞书')
          if (selected.includes('wecom') && !wecomConfigured) unconfigured.push('企业微信')
          if (unconfigured.length === 0) return null
          return (
            <p className="text-[10px] leading-relaxed text-warning/80">
              {unconfigured.join('、')}尚未配置,
              <Link to="/settings?tab=monitoring" className="text-accent hover:text-accent/80">前往设置页配置 →</Link>
            </p>
          )
        })()}
        {(draft.webhook_channels ?? []).length > 0 && (() => {
          const selected = draft.webhook_channels ?? []
          const ready: string[] = []
          if (selected.includes('feishu') && feishuConfigured) ready.push('飞书')
          if (selected.includes('wecom') && wecomConfigured) ready.push('企业微信')
          if (ready.length === 0) return null
          return (
            <p className="text-[10px] leading-relaxed text-muted">
              命中本规则时,告警将推送到已配置的{ready.join(' + ')}。
            </p>
          )
        })()}
      </div>

      {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}

      <div className="flex justify-end gap-2">
        <button onClick={onClose} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs cursor-pointer">取消</button>
        <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-accent text-base text-xs font-medium disabled:opacity-50 cursor-pointer">
          <Save className="h-3.5 w-3.5" />保存
        </button>
      </div>
    </div>
  )
}
