import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, CircleAlert, CircleCheck, FlaskConical, PenLine, Play, Save, ShieldQuestion } from 'lucide-react'
import { toast } from '@/components/Toast'
import { api, type FactorTrialResponse, type FactorValidateResponse } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS = 'w-full rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs focus:border-accent focus:outline-none'
const CHIP_CLS = 'shrink-0 rounded-btn border border-border bg-base/60 px-1.5 py-0.5 font-mono text-[10px] text-secondary transition-colors hover:border-accent/40 hover:text-accent cursor-pointer'

const DEFAULT_FORMULA = 'rank(-ts_sum(change_pct, 5))'

const TEMPLATES: { label: string; formula: string; note: string }[] = [
  { label: '5日反转', formula: 'rank(-ts_sum(change_pct, 5))', note: '近 5 日累计涨幅的截面倒数' },
  { label: '量价相关', formula: 'ts_corr(close / ts_delay(close, 1) - 1, volume, 20)', note: '日收益与成交量的 20 日滚动相关' },
  { label: '波动变化', formula: 'ts_std(close / ts_delay(close, 1) - 1, 20)', note: '20 日滚动日收益波动' },
  { label: '换手异动', formula: 'ts_zscore(turnover_rate, 60)', note: '换手率相对自身 60 日分布的 z 分' },
  { label: '乖离组合', formula: 'zscore(close / ma20 - 1) - zscore(ts_mean(turnover_rate, 5))', note: '价格乖离与换手均值的截面差' },
  { label: '低波动', formula: 'rank(-ts_std(change_pct, 20))', note: '20 日日收益波动最低 (低波动异象)' },
  { label: '量比回落', formula: 'rank(-ts_zscore(vol_ratio_5d, 60))', note: '量比回落到自身 60 日低位' },
  { label: 'RSI超卖', formula: 'rank(-ts_mean(rsi_14, 5))', note: '近 5 日 RSI 均值最低的超卖标的' },
  { label: '低振幅', formula: 'rank(-ts_mean(amplitude, 20))', note: '20 日平均振幅最低 (缩量休整)' },
  { label: '隔夜反转', formula: 'rank(-overnight_ret_20d)', note: '20 日累计隔夜收益的反转' },
  { label: '距高点回落', formula: 'rank(-distance_to_high_60d)', note: '距 60 日高点回撤最深的标的' },
  { label: '换手稳定', formula: 'rank(-ts_std(turnover_rate, 20))', note: '换手率波动最低 (筹码稳定)' },
]

/** 后端 dsl.OPERATORS 的全部 25 个算子, 按家族分组; name=chip 名, sig=完整签名, desc=中文说明。
 *  chips 面板负责点击插入, 速查表负责阅读 — 同一份数据两个视图。 */
const OPERATOR_GROUPS: { label: string; ops: { name: string; sig: string; desc: string }[] }[] = [
  {
    label: '时序', ops: [
      { name: 'ts_mean', sig: 'ts_mean(x,n)', desc: '滚动均值 · n∈[2,512]' },
      { name: 'ts_sum', sig: 'ts_sum(x,n)', desc: '滚动求和 · n∈[2,512]' },
      { name: 'ts_std', sig: 'ts_std(x,n)', desc: '滚动标准差 · n∈[2,512]' },
      { name: 'ts_max', sig: 'ts_max(x,n)', desc: '滚动最大值 · n∈[2,512]' },
      { name: 'ts_min', sig: 'ts_min(x,n)', desc: '滚动最小值 · n∈[2,512]' },
      { name: 'ts_delay', sig: 'ts_delay(x,n)', desc: 'n 期前的值 · n≤512' },
      { name: 'ts_delta', sig: 'ts_delta(x,n)', desc: 'n 期差分 · x − n 期前' },
      { name: 'ts_rank', sig: 'ts_rank(x,n)', desc: '滚动分位' },
      { name: 'ts_zscore', sig: 'ts_zscore(x,n)', desc: '滚动 z 分' },
      { name: 'ts_corr', sig: 'ts_corr(x,y,n)', desc: '滚动相关系数' },
      { name: 'ts_cov', sig: 'ts_cov(x,y,n)', desc: '滚动协方差' },
      { name: 'ts_quantile', sig: 'ts_quantile(x,n,q)', desc: '滚动分位 · q∈(0,1)' },
      { name: 'decay_linear', sig: 'decay_linear(x,n)', desc: '线性衰减加权 · 近端权重大' },
    ],
  },
  {
    label: '截面', ops: [
      { name: 'rank', sig: 'rank(x)', desc: '当日横截面百分位' },
      { name: 'zscore', sig: 'zscore(x)', desc: '当日横截面 z 分' },
      { name: 'winsorize', sig: 'winsorize(x,k)', desc: '截面截尾至 μ±kσ · k∈[1,6] 默认 3' },
    ],
  },
  {
    label: '工具', ops: [
      { name: 'if_else', sig: 'if_else(c,a,b)', desc: '条件选择' },
      { name: 'min', sig: 'min(a,b)', desc: '两值取小' },
      { name: 'max', sig: 'max(a,b)', desc: '两值取大' },
      { name: 'log', sig: 'log(x)', desc: '自然对数' },
      { name: 'abs', sig: 'abs(x)', desc: '绝对值' },
      { name: 'sign', sig: 'sign(x)', desc: '符号函数' },
      { name: 'sqrt', sig: 'sqrt(x)', desc: '平方根' },
      { name: 'power', sig: 'power(x,c)', desc: '幂运算 · |c|≤4' },
      { name: 'clamp', sig: 'clamp(x,lo,hi)', desc: '截断到 [lo,hi]' },
    ],
  },
]
const OPERATOR_COUNT = OPERATOR_GROUPS.reduce((n, g) => n + g.ops.length, 0)

interface EditingFactor {
  id: string
  label: string
  group: string
  formula: string
  description: string
  direction: string
  version: number
}

/** 因子编辑器: 写公式 → 校验 → 试算 → 保存; 支持编辑已有自定义因子 (版本提升)。 */
export function FactorEditor({ editId = '' }: { editId?: string }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [formula, setFormula] = useState(DEFAULT_FORMULA)
  const [assetType, setAssetType] = useState<'stock' | 'etf'>('stock')
  const [validation, setValidation] = useState<FactorValidateResponse | null>(null)
  const [trial, setTrial] = useState<FactorTrialResponse | null>(null)
  const [saveLabel, setSaveLabel] = useState('')
  const [saveId, setSaveId] = useState('')
  const [saveGroup, setSaveGroup] = useState('自定义')
  const [saveDescription, setSaveDescription] = useState('')
  const [direction, setDirection] = useState<'none' | 'high' | 'low'>('none')
  const [editing, setEditing] = useState<EditingFactor | null>(null)
  const [savedId, setSavedId] = useState('')
  const [savedVersion, setSavedVersion] = useState<number | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const library = useQuery({
    queryKey: QK.factorLibrary('all'),
    queryFn: () => api.factorLibrary(),
    staleTime: 30_000,
  })
  const columns = useQuery({
    queryKey: QK.factorColumns,
    queryFn: api.factorColumns,
    staleTime: 300_000,
  })
  const customFactors = (library.data?.factors ?? []).filter(item => item.kind === 'custom')
  const fieldGroups = useMemo(() => {
    const cols = columns.data?.columns ?? []
    const groups: Record<string, typeof cols> = {}
    for (const col of cols) (groups[col.group] ??= []).push(col)
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b, 'zh'))
  }, [columns.data])

  const loadForEdit = (id: string) => {
    const item = customFactors.find(f => f.id === id)
    if (!item) return
    setEditing({
      id: item.id, label: item.label, group: item.group,
      formula: item.formula, description: '', direction: item.direction,
      version: item.version,
    })
    setFormula(item.formula)
    setSaveLabel(item.label)
    setSaveGroup(item.group)
    setSaveId(item.id)
    setDirection((item.direction === 'high' || item.direction === 'low') ? item.direction : 'none')
    setValidation(null)
    setTrial(null)
    setSavedId('')
    setSavedVersion(null)
  }

  // URL ?edit= 透传 (因子库「编辑公式」入口)
  useEffect(() => {
    if (editId && customFactors.length && !editing) {
      if (customFactors.some(f => f.id === editId)) loadForEdit(editId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editId, customFactors.length])

  const validate = useMutation({
    mutationFn: () => api.factorValidate(formula),
    onSuccess: data => setValidation(data),
  })
  const runTrial = useMutation({
    mutationFn: () => api.factorTrial({ formula, asset_type: assetType, days: 40 }),
    onSuccess: data => setTrial(data),
  })
  const save = useMutation({
    mutationFn: () => {
      const payload = {
        label: saveLabel.trim(),
        group: saveGroup.trim() || '自定义',
        formula,
        description: saveDescription.trim(),
        direction,
      }
      return editing
        ? api.factorCustomUpdate(editing.id, payload)
        : api.factorCustomCreate({ ...payload, id: saveId.trim() || undefined })
    },
    onSuccess: data => {
      setSavedId(data.id)
      setSavedVersion(data.version)
      if (editing) setEditing({ ...editing, version: data.version })
      toast(editing
        ? `${data.id} 已更新到 v${data.version}${'status' in data && data.status === 'draft' ? ' (公式变化, 状态回草稿)' : ''}`
        : `已注册自定义因子 ${data.id} (v${data.version})，可在因子库与检验页使用`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['factors-library'] })
    },
    onError: (error: Error) => toast(`保存失败 · ${error.message}`, 'error'),
  })

  const runBoth = () => {
    setTrial(null)
    validate.mutate()
  }
  const canTrial = validation?.ok === true && !runTrial.isPending
  const formulaDirty = editing != null && editing.formula !== formula

  // 点击错误 → 聚焦并选中出错位置附近的片段
  const locateError = (offset: number | undefined) => {
    if (offset == null || !textareaRef.current) return
    const start = Math.max(0, Math.min(offset, formula.length - 1))
    const end = Math.min(formula.length, start + 12)
    textareaRef.current.focus()
    textareaRef.current.setSelectionRange(start, end)
  }

  // 算子/字段 chip → 插入到公式光标处 (无光标则追加到末尾)
  const insertSnippet = (snippet: string) => {
    const el = textareaRef.current
    if (!el) {
      setFormula(formula + snippet)
      return
    }
    const start = el.selectionStart ?? formula.length
    const end = el.selectionEnd ?? start
    const next = formula.slice(0, start) + snippet + formula.slice(end)
    setFormula(next)
    setValidation(null)
    setTrial(null)
    setSavedId('')
    requestAnimationFrame(() => {
      el.focus()
      const pos = start + snippet.length
      el.setSelectionRange(pos, pos)
    })
  }

  // 模板/我的因子整体替换公式; 非初始内容时先确认, 防误触丢失半成品
  const applyTemplate = (next: string) => {
    const untouched = !editing && (formula === DEFAULT_FORMULA || formula.trim() === '')
    if (!untouched && !window.confirm('当前公式将被整体替换, 继续?')) return
    setFormula(next)
    setValidation(null)
    setTrial(null)
    setSavedId('')
  }

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-3 overflow-hidden xl:grid-cols-2">
      {/* ── 左栏: 编辑 ── */}
      <section className="flex min-h-0 flex-col gap-3 rounded-card border border-border bg-surface/80 p-3 xl:overflow-y-auto">
        <div className="flex flex-wrap items-center gap-2">
          <FlaskConical className="h-4 w-4 shrink-0 text-accent" />
          <span className="shrink-0 text-sm font-medium text-foreground">因子编辑器</span>
          {editing && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-btn border border-accent/40 bg-accent/5 px-1.5 py-0.5 text-[10px] font-medium text-accent">
              <PenLine className="h-3 w-3" />
              编辑 {editing.id} · 当前 v{editing.version}
            </span>
          )}
          {customFactors.length > 0 && (
            <select
              value={editing?.id ?? ''}
              onChange={event => {
                if (!event.target.value) { setEditing(null); return }
                loadForEdit(event.target.value)
              }}
              className="ml-auto h-7 max-w-[14rem] shrink-0 rounded-input border border-border bg-surface px-1.5 text-[10px] text-secondary"
              aria-label="编辑已有自定义因子"
            >
              <option value="">编辑已有因子…</option>
              {customFactors.map(item => (
                <option key={item.id} value={item.id}>{item.label} ({item.id} v{item.version})</option>
              ))}
            </select>
          )}
        </div>

        <div>
          <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
            <label className="shrink-0 text-xs font-medium text-secondary">公式 (DSL)</label>
            <select
              className="h-7 shrink-0 rounded-input border border-border bg-surface px-1.5 text-[10px] text-secondary"
              onChange={event => {
                const value = event.target.value
                if (!value) return
                const mine = value.startsWith('my:') ? customFactors.find(f => f.id === value.slice(3)) : undefined
                const template = TEMPLATES.find(item => item.label === value)
                const next = mine?.formula ?? template?.formula
                if (next != null) applyTemplate(next)
                event.target.value = ''
              }}
              aria-label="从模板开始"
            >
              <option value="">从模板开始 / 我的因子…</option>
              <optgroup label="经典模板">
                {TEMPLATES.map(template => (
                  <option key={template.label} value={template.label}>{template.label} · {template.note}</option>
                ))}
              </optgroup>
              {customFactors.length > 0 && (
                <optgroup label={`我的因子 (${customFactors.length})`}>
                  {customFactors.map(item => (
                    <option key={item.id} value={`my:${item.id}`}>{item.label} ({item.id})</option>
                  ))}
                </optgroup>
              )}
            </select>
          </div>
          <textarea
            ref={textareaRef}
            value={formula}
            onChange={event => { setFormula(event.target.value); setValidation(null); setTrial(null); setSavedId('') }}
            spellCheck={false}
            rows={5}
            className="w-full resize-y rounded-input border border-border bg-base/60 px-3 py-2 font-mono text-xs leading-relaxed text-foreground focus:border-accent focus:outline-none"
            placeholder="例如: rank(-ts_sum(change_pct, 5))"
          />
        </div>

        {/* 算子/字段点选插入: 写公式不用背名字 */}
        <div className="space-y-1.5">
          <details
            ref={el => { if (el) el.open = true }}
            className="group rounded-btn border border-border/60 bg-base/40 px-2.5 py-1.5 text-[10px]"
          >
            <summary className="flex cursor-pointer select-none items-center gap-1 font-medium text-foreground transition-colors hover:text-accent [&::-webkit-details-marker]:hidden">
              <ChevronRight className="h-3 w-3 shrink-0 text-accent transition-transform group-open:rotate-90" />
              算子（{OPERATOR_COUNT} 个 · 时序 / 截面 / 工具）
              <span className="font-normal text-muted">· 点击签名插入</span>
            </summary>
            <div className="mt-1.5 grid grid-cols-1 gap-2 sm:grid-cols-2" role="group" aria-label="插入算子">
              {OPERATOR_GROUPS.map(group => (
                <div key={group.label} className="rounded-btn border border-border/50 bg-base/30 px-2 py-1.5">
                  <div className="mb-1 font-medium text-secondary">{group.label} <span className="font-normal text-muted">({group.ops.length})</span></div>
                  <div className="space-y-0.5">
                    {group.ops.map(op => (
                      <button
                        key={op.name}
                        type="button"
                        title={`点击插入 ${op.sig} — ${op.desc}`}
                        onClick={() => insertSnippet(`${op.name}(`)}
                        className="flex w-full cursor-pointer items-baseline gap-2 rounded px-1 py-px text-left leading-4 transition-colors hover:bg-accent/10"
                      >
                        <code className="w-36 shrink-0 truncate font-mono text-accent">{op.sig}</code>
                        <span className="text-muted">{op.desc}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-1.5 text-muted">四则运算 <code className="font-mono text-accent">+ - * /</code> · 除零→空值 · 括号可任意分组</div>
          </details>
          <details className="group rounded-btn border border-border/60 bg-base/40 px-2.5 py-1.5 text-[10px]">
            <summary className="flex cursor-pointer select-none items-center gap-1 font-medium text-foreground transition-colors hover:text-accent [&::-webkit-details-marker]:hidden">
              <ChevronRight className="h-3 w-3 shrink-0 text-accent transition-transform group-open:rotate-90" />
              可用字段 ({columns.data?.columns.length ?? 0}) · 点击插入
            </summary>
            <div className="mt-1.5 max-h-64 space-y-1.5 overflow-y-auto">
              {fieldGroups.map(([group, cols]) => (
                <div key={group}>
                  <div className="mb-0.5 text-[10px] font-medium text-secondary">{group}</div>
                  <div className="flex flex-wrap gap-1">
                    {cols.map(col => (
                      <button
                        key={col.id}
                        type="button"
                        title={`${col.label} — ${col.desc}`}
                        onClick={() => insertSnippet(col.id)}
                        className={CHIP_CLS}
                      >
                        <span className="font-sans text-secondary">{col.label}</span>
                        <span className="ml-1 font-mono text-muted/70">{col.id}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              {columns.isPending && <div className="text-muted">加载字段中…</div>}
            </div>
          </details>
        </div>

        {/* 动作行: nowrap 防止按钮文字折行 */}
        <div className="flex flex-wrap items-center gap-2">
          <select value={assetType} onChange={event => setAssetType(event.target.value as typeof assetType)} className={`${INPUT_CLS} h-8 w-24 shrink-0`} aria-label="试算资产">
            <option value="stock">股票</option>
            <option value="etf">ETF</option>
          </select>
          <button
            type="button"
            onClick={runBoth}
            disabled={!formula.trim() || validate.isPending}
            className="ml-auto inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-btn border border-border bg-surface px-3 text-xs font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
          >
            <ShieldQuestion className="h-3.5 w-3.5" />
            {validate.isPending ? '校验中…' : '校验'}
          </button>
          <button
            type="button"
            onClick={() => runTrial.mutate()}
            disabled={!canTrial}
            className="inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
            title={validation?.ok ? '用最近 40 个交易日数据试算 IC' : '请先校验通过'}
          >
            <Play className="h-3.5 w-3.5" />
            {runTrial.isPending ? '试算中…' : '试算 40 日'}
          </button>
        </div>

        {validate.isError && (
          <div className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
            {String((validate.error as Error).message)}
          </div>
        )}
      </section>

      {/* ── 右栏: 校验 / 试算 / 保存 ── */}
      <section className="flex min-h-0 flex-col gap-3 rounded-card border border-border bg-surface/80 p-3 xl:overflow-y-auto">
        <div className="text-sm font-medium text-foreground">校验与试算</div>

        {!validation && !validate.isPending && (
          <div className="rounded-btn border border-dashed border-border px-3 py-6 text-center text-xs text-muted">
            <div className="mb-2">写好公式后按顺序走完四步：</div>
            <div className="flex flex-wrap items-center justify-center gap-x-1.5 gap-y-1">
              <span className="rounded-btn bg-elevated px-2 py-1">1 写公式</span>
              <span>→</span>
              <span className="rounded-btn bg-elevated px-2 py-1">2 校验语法</span>
              <span>→</span>
              <span className="rounded-btn bg-elevated px-2 py-1">3 试算 IC</span>
              <span>→</span>
              <span className="rounded-btn bg-elevated px-2 py-1">4 保存草稿</span>
            </div>
            <div className="mt-2 text-[10px]">算子/字段在左侧点击插入; 不知从何写起可从模板或「我的因子」开始; 校验错误可点击定位到字符。</div>
          </div>
        )}

        {validation && (
          <div className="space-y-2">
            {validation.ok ? (
              <div className="flex items-center gap-2 rounded-btn border border-bull/30 bg-bull/10 px-3 py-2 text-xs text-bull">
                <CircleCheck className="h-4 w-4 shrink-0" />
                公式有效。预热 {validation.warmup_bars} 个交易日
                {validation.cross_sectional && '，含截面算子。'}
              </div>
            ) : (
              <div className="space-y-1.5 rounded-btn border border-danger/30 bg-danger/10 px-3 py-2">
                <div className="flex items-center gap-2 text-xs font-medium text-danger">
                  <CircleAlert className="h-4 w-4 shrink-0" />公式未通过校验 (点击错误可定位):
                </div>
                {validation.errors.map((error, index) => (
                  <button
                    key={index}
                    type="button"
                    onClick={() => locateError(error.position?.offset)}
                    className="block w-full text-left font-mono text-[10px] leading-relaxed text-danger transition-colors hover:text-danger/80"
                    title={error.position != null ? `定位到第 ${error.position.offset} 个字符附近` : undefined}
                  >
                    [{error.code}] {error.message}
                  </button>
                ))}
              </div>
            )}
            {validation.ok && (
              <div className="grid grid-cols-1 gap-2 text-[10px] sm:grid-cols-2">
                <div className="rounded-btn border border-border bg-base/40 px-2.5 py-1.5">
                  <div className="mb-0.5 text-muted">依赖列 (递归展开)</div>
                  <code className="break-all font-mono text-secondary">{validation.dependencies.join(', ') || '—'}</code>
                </div>
                <div className="rounded-btn border border-border bg-base/40 px-2.5 py-1.5">
                  <div className="mb-0.5 text-muted">引用因子</div>
                  <code className="break-all font-mono text-secondary">{validation.referenced_factors.join(', ') || '—'}</code>
                </div>
              </div>
            )}
          </div>
        )}

        {runTrial.isPending && (
          <div className="flex items-center gap-3 rounded-btn border border-accent/30 bg-accent/5 px-3 py-2.5 text-xs text-secondary">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent/25 border-t-accent" />
            正在加载面板并试算最近 40 个交易日…
          </div>
        )}
        {runTrial.isError && (
          <div className="rounded-btn border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
            {String((runTrial.error as Error).message)}
          </div>
        )}

        {trial && !runTrial.isPending && (
          <div className="space-y-2">
            {trial.n_dates === 0 ? (
              <div className="rounded-btn border border-border bg-base/40 px-3 py-2 text-xs text-muted">
                {trial.message ?? '无有效 IC 截面。'}
              </div>
            ) : (
              <>
                <div className="grid grid-cols-3 gap-2 text-center sm:grid-cols-5">
                  {([
                    ['IC 均值', trial.ic_mean?.toFixed(4) ?? '—'],
                    ['ICIR', trial.ir?.toFixed(2) ?? '—'],
                    ['IC 胜率', trial.ic_win_rate != null ? `${(trial.ic_win_rate * 100).toFixed(0)}%` : '—'],
                    ['t值(NW)', trial.t_newey_west != null ? trial.t_newey_west.toFixed(2) : '—'],
                    ['空值率', trial.null_ratio != null ? `${(trial.null_ratio * 100).toFixed(0)}%` : '—'],
                  ] as [string, string][]).map(([label, value]) => (
                    <div key={label} className="rounded-btn border border-border bg-base/40 px-1 py-1.5" title={label === 't值(NW)' ? 'Newey-West 稳健 t 值 (滞后1), |t|≥2 视为显著; 40 日样本仅作参考' : undefined}>
                      <div className="whitespace-nowrap text-[10px] text-muted">{label}</div>
                      <div className="font-mono text-sm text-foreground">{value}</div>
                    </div>
                  ))}
                </div>
                <div className="rounded-btn border border-border bg-base/40 px-3 py-2">
                  <div className="mb-1.5 text-[10px] text-muted">IC 走势 (最近 {trial.ic_series.length} 日)</div>
                  <div className="flex h-16 items-center gap-px">
                    {trial.ic_series.map(point => {
                      const height = Math.min(Math.abs(point.ic) * 160, 100)
                      return (
                        <div key={point.date} className="flex h-full flex-1 flex-col justify-center" title={`${point.date}: ${point.ic} (${point.n_symbols}只)`}>
                          <div className="flex h-1/2 items-end"><div className="w-full rounded-t-sm bg-bull/70" style={{ height: point.ic > 0 ? `${height / 2}%` : 0 }} /></div>
                          <div className="flex h-1/2 items-start"><div className="w-full rounded-b-sm bg-bear/70" style={{ height: point.ic < 0 ? `${height / 2}%` : 0 }} /></div>
                        </div>
                      )
                    })}
                  </div>
                </div>
                <div className="text-[10px] leading-relaxed text-muted">
                  试算仅为快照预览 (无成本/分层), 完整检验请到「发现」页运行。样本内表现不代表未来。
                </div>

                {/* 注册/更新区: 试算有非空输出才可保存 (服务端同样 fail-closed 校验) */}
                {(editing || (trial.ok && trial.n_dates > 0)) && (
                  <div className="rounded-btn border border-border bg-base/40 p-2.5">
                    <div className="mb-1.5 text-[11px] font-medium text-secondary">
                      {editing ? `更新 ${editing.id} (保存为新版本 v${editing.version + 1})` : '注册为自定义因子'}
                    </div>
                    <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                      <input
                        type="text"
                        value={saveLabel}
                        onChange={event => { setSaveLabel(event.target.value); setSavedId('') }}
                        placeholder="名称 (必填, 如 我的反转)"
                        className={`${INPUT_CLS} h-8`}
                      />
                      <input
                        type="text"
                        value={editing ? saveGroup : saveId}
                        onChange={event => {
                          if (editing) setSaveGroup(event.target.value)
                          else setSaveId(event.target.value)
                        }}
                        placeholder={editing ? '分组' : 'id (可空, 自动生成 uf_ 前缀)'}
                        className={`${INPUT_CLS} h-8 font-mono`}
                      />
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      <select value={direction} onChange={event => setDirection(event.target.value as typeof direction)} className={`${INPUT_CLS} h-8 w-32 shrink-0`} aria-label="预期方向">
                        <option value="none">方向未知</option>
                        <option value="high">值大看多</option>
                        <option value="low">值小看多</option>
                      </select>
                      <input
                        type="text"
                        value={saveDescription}
                        onChange={event => setSaveDescription(event.target.value)}
                        placeholder="描述 (可空, 记录设计意图, 如: 短线超跌反转)"
                        className={`${INPUT_CLS} h-8 min-w-[12rem] flex-1`}
                        maxLength={500}
                      />
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => save.mutate()}
                        disabled={!saveLabel.trim() || save.isPending}
                        className="inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-btn bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <Save className="h-3.5 w-3.5" />
                        {save.isPending ? '保存中…' : editing ? (formulaDirty ? '保存新版本 (回草稿)' : '保存元数据') : '保存 (草稿态)'}
                      </button>
                      {savedId && !save.isPending && (
                        <button
                          type="button"
                          onClick={() => navigate(`/factors?tab=discover&focus=${savedId}`)}
                          className="inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-btn border border-accent/40 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/10"
                        >
                          去检验 {savedId}
                        </button>
                      )}
                    </div>
                    <div className="mt-1 text-[10px] text-muted">
                      {editing
                        ? '公式变化会保存为新版本并回草稿态 (需重新检验后激活); 仅改名称/分组保留当前状态。'
                        : '保存后状态为 draft；到「发现」页跑完整检验确认有效后，在因子库中激活。'}
                    </div>
                    {savedId && savedVersion != null && (
                      <div className="mt-1 text-[10px] text-bull">已保存: {savedId} v{savedVersion}</div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
