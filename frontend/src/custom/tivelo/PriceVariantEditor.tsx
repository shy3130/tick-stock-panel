import { useId, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { PRO_DYNAMIC_PRICE_PROVIDER, type EngineInputCatalog, type EnginePriceChoice, type EnginePriceVariant } from '@/lib/engineJobTypes'
import { QK } from '@/lib/queryKeys'
import { Button, ErrorNotice, inputClass } from './ui'
import { priceLabel } from './viewModel'

export type CatalogRun = EngineInputCatalog['runs'][number]

const builtins: EnginePriceChoice[] = (['raw', 'qfq', 'hfq'] as const).map(mode => ({
  provider_id: 'tivelo.' + mode, mode, state: 'available',
}))
const states: Record<EnginePriceChoice['state'], string> = {
  available: '', not_installed: '待导入扩展', disabled: '待启用', incompatible: '版本不可用',
  needs_license: '待导入授权', license_expired: '授权已过期',
}

function validUtc(value: string) {
  if (!/^(?!0000)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value)) return false
  const date = new Date(value)
  return Number.isFinite(date.valueOf()) && date.toISOString().slice(0, 19) === value.slice(0, 19)
}

export function PriceVariantEditor({ source, snapshot, saving, onSave, onCancel }: {
  source: CatalogRun
  snapshot: string
  saving: boolean
  onSave: (request: EnginePriceVariant) => Promise<void>
  onCancel?: () => void
}) {
  const id = useId()
  const query = useQuery({ queryKey: QK.enginePriceViews, queryFn: api.enginePriceViews, retry: false, staleTime: 0 })
  const choices = query.data ?? builtins
  const [provider, setProvider] = useState(source.price_view.provider_id)
  const [factor, setFactor] = useState(source.price_view.factor_definition_id ?? '')
  const [baseline, setBaseline] = useState(source.price_view.factor_baseline_id ?? '')
  const [anchor, setAnchor] = useState(source.price_view.anchor_at?.replace(/\+00:00$/, 'Z') ?? '')
  const [adapt, setAdapt] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [failure, setFailure] = useState<unknown>(null)
  const choice = choices.find(item => item.provider_id === provider)
  const isDynamic = provider === PRO_DYNAMIC_PRICE_PROVIDER
  const isFixed = choice?.mode === 'qfq' || choice?.mode === 'hfq'
  const custom = choice?.mode === 'extension' && !isDynamic
  const numeric = choice?.mode === 'extension' ? 'projected_prices_float64_v1' : 'anchor_cancelled_float64_v1'
  const needsAdapt = !!source.controlled_semantics && source.controlled_semantics !== numeric
  const locked = choice?.state !== 'available'
  const manage = choices.some(item => item.mode === 'extension' && item.state !== 'available')

  function edited() {
    setErrors({})
    setFailure(null)
  }
  async function save() {
    if (!choice || locked || saving) return
    const invalid: Record<string, string> = {}
    if ((isFixed || isDynamic) && !factor.trim()) invalid.factor = '填写快照使用的因子定义。'
    if ((isFixed || isDynamic) && !baseline.trim()) invalid.baseline = '填写同一因子序列的基准。'
    if ((isFixed || (custom && anchor.trim())) && !validUtc(anchor.trim())) invalid.anchor = '填写有效的 UTC 日期与时间，例如 2025-01-02T01:30:00Z。'
    if (needsAdapt && !adapt) invalid.adapt = '请先确认按新口径另存策略。'
    setErrors(invalid)
    setFailure(null)
    if (Object.keys(invalid).length) {
      document.getElementById(id + '-' + Object.keys(invalid)[0])?.focus()
      return
    }
    const view: EnginePriceVariant['price_view'] = { mode: choice.mode, provider_id: provider }
    if (choice.mode !== 'raw') {
      if (factor.trim()) view.factor_definition_id = factor.trim()
      if (baseline.trim()) view.factor_baseline_id = baseline.trim()
      if (!isDynamic && anchor.trim()) view.anchor_at = anchor.trim()
    }
    try {
      await onSave({ run: source.path, snapshot, source_lock_id: source.lock_id, price_view: view, adapt_controlled: needsAdapt && adapt })
    } catch (error) {
      setFailure(error)
    }
  }
  function field(key: string, label: string, value: string, set: (value: string) => void) {
    const name = id + '-' + key
    return <div className="min-w-0 space-y-1">
      <label htmlFor={name} className="text-sm text-secondary">{label}</label>
      <input id={name} className={inputClass} value={value} maxLength={256} spellCheck={false}
        aria-invalid={!!errors[key]} aria-describedby={errors[key] ? name + '-error' : undefined}
        placeholder={key === 'anchor' ? '2025-01-02T01:30:00Z' : undefined}
        onChange={event => { edited(); set(event.target.value) }} />
      {errors[key] && <p id={name + '-error'} className="text-sm text-red-700 dark:text-danger">{errors[key]}</p>}
    </div>
  }
  return <fieldset className="min-w-0 space-y-4 border-l-2 border-border pl-4">
    <legend className="mb-2 px-1 text-sm font-medium">更换价格口径</legend>
    <div className="space-y-1">
      <label htmlFor={id + '-view'} className="text-sm text-secondary">新价格口径</label>
      <select id={id + '-view'} className={inputClass} value={provider}
        onChange={event => { edited(); setProvider(event.target.value); setAdapt(false) }}>
        {!choice && <option value={provider} disabled>{priceLabel(source.price_view.mode, provider)} · 待读取扩展</option>}
        {choices.map(item => <option key={item.provider_id} value={item.provider_id}>
          {priceLabel(item.mode, item.provider_id)}{states[item.state] && ' · ' + states[item.state]}
        </option>)}
      </select>
      {choice?.version && <p className="text-sm text-secondary">活动版本 {choice.version}</p>}
    </div>
    {query.isPending && <p role="status" className="text-sm text-secondary">正在读取扩展与本机授权…</p>}
    {query.isError && <p role="alert" className="text-sm text-secondary">扩展状态读取失败。<button type="button" className="text-accent underline" onClick={() => void query.refetch()}>重新读取</button></p>}
    {manage && <Link className="inline-flex min-h-11 items-center text-sm text-accent underline underline-offset-4"
      to={choice?.state === 'not_installed' ? '/settings?tab=import' : '/settings?tab=import&view=installed'}>前往导入中心管理扩展与授权</Link>}
    {choice && !locked && <>
      {choice.mode !== 'raw' && <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        {field('factor', custom ? '因子定义（选填）' : '因子定义', factor, setFactor)}
        {field('baseline', custom ? '因子基准（选填）' : '因子基准', baseline, setBaseline)}
      </div>}
      {(isFixed || custom) && field('anchor', custom ? '固定锚点（UTC，选填）' : '固定锚点（UTC）', anchor, setAnchor)}
      {isDynamic && <p className="text-sm leading-6 text-secondary">扩展在每个决策时点使用当时可见的因子。</p>}
      {needsAdapt && <div className="space-y-1">
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm" htmlFor={id + '-adapt'}>
          <input id={id + '-adapt'} type="checkbox" checked={adapt} aria-invalid={!!errors.adapt}
            aria-describedby={id + (errors.adapt ? '-adapt-error' : '-adapt-help')}
            onChange={event => { edited(); setAdapt(event.target.checked) }} />
          按新口径另存受控策略
        </label>
        <p id={id + '-adapt-help'} className="text-sm leading-6 text-secondary">公式沿用原策略，价格计算方式会改变，交易信号可能不同。</p>
        {errors.adapt && <p id={id + '-adapt-error'} className="text-sm text-red-700 dark:text-danger">{errors.adapt}</p>}
      </div>}
    </>}
    {failure != null && <ErrorNotice error={failure} />}
    <div className="flex flex-wrap gap-2">
      <Button variant="primary" disabled={locked || saving} onClick={() => void save()}>{saving ? '正在保存…' : '另存回测输入'}</Button>
      {onCancel && <Button variant="quiet" onClick={onCancel} disabled={saving}>取消更换</Button>}
    </div>
  </fieldset>
}
