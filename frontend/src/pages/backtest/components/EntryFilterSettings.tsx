import { useQuery } from '@tanstack/react-query'
import { Plus, X } from 'lucide-react'
import { api, REGIME_STATE_LABELS, type ExtDataConfig } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type { EntryFilters, ExtensionEntryCondition, MarketFilterSource } from '@/lib/entryFilterTypes'

const SELECT = 'h-9 w-full min-w-0 rounded-input border border-border bg-base px-2 text-xs text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'
const SECONDARY = 'inline-flex min-h-8 items-center gap-1 rounded-btn border border-border px-2 py-1 text-xs text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50'

type Operator = ExtensionEntryCondition['op']
const NUMERIC_OPERATORS: Operator[] = ['>=', '>', '<=', '<', '==', '!=']
const TEXT_OPERATORS: Operator[] = ['contains', '==', '!=']
const LABELS: Record<Operator, string> = {
  '>=': '大于等于', '>': '大于', '<=': '小于等于', '<': '小于',
  '==': '等于', '!=': '不等于', contains: '包含',
}

const availableFields = (table: ExtDataConfig) => table.fields.filter(
  field => field.name !== 'symbol' && field.name !== 'available_at' && ['int', 'float', 'string'].includes(field.dtype),
)
const keyOf = (table: string, field: string) => JSON.stringify([table, field])

function ExtensionConditions({ tables, rules, onChange, market, loading, error }: {
  tables: ExtDataConfig[]
  rules: ExtensionEntryCondition[]
  onChange: (rules: ExtensionEntryCondition[]) => void
  market?: boolean
  loading: boolean
  error: boolean
}) {
  const choices = tables.flatMap(table => availableFields(table).map(field => ({
    key: keyOf(table.id, field.name), table, field,
  })))
  const prefix = market ? '市场' : '个股'
  function change(index: number, patch: Partial<ExtensionEntryCondition>) {
    onChange(rules.map((rule, i) => i === index ? { ...rule, ...patch } : rule))
  }
  return (
    <div className="space-y-2">
      {rules.map((rule, index) => {
        const key = keyOf(rule.table_id, rule.field)
        const choice = choices.find(option => option.key === key)
        const text = choice ? choice.field.dtype === 'string' : rule.op === 'contains' || rule.op === '==' || rule.op === '!='
        return (
          <div key={index} className="grid grid-cols-[minmax(0,1fr)_2.25rem] gap-2 rounded-btn border border-border bg-surface/60 p-2 sm:grid-cols-[minmax(0,1.3fr)_minmax(0,.85fr)_minmax(0,1fr)_2.25rem]">
            <select aria-label={`${prefix}字段 ${index + 1}`} value={key} className={`${SELECT} col-start-1 sm:col-auto`}
              onChange={event => {
                const next = choices.find(option => option.key === event.target.value)
                if (next) change(index, {
                  table_id: next.table.id, field: next.field.name,
                  op: next.field.dtype === 'string' ? '==' : '>=', value: '',
                })
              }}
            >
              {!choice && <option value={key}>{rule.table_id} · {rule.field}</option>}
              {tables.map(table => (
                <optgroup label={table.label} key={table.id}>
                  {availableFields(table).map(field => (
                    <option key={field.name} value={keyOf(table.id, field.name)}>{field.label || field.name}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <div className="col-span-2 flex gap-2 sm:contents">
              <select aria-label={`${prefix}比较方式 ${index + 1}`} className={SELECT} value={rule.op}
                onChange={event => change(index, { op: event.target.value as Operator })}>
                {(text ? TEXT_OPERATORS : NUMERIC_OPERATORS).map(op => <option value={op} key={op}>{LABELS[op]}</option>)}
              </select>
              <input aria-label={`${prefix}条件值 ${index + 1}`} className={SELECT}
                type={text ? 'text' : 'number'} step="any" maxLength={512} value={rule.value}
                placeholder={text ? '输入标签或状态' : '输入数值'}
                onChange={event => change(index, { value: event.target.value })} />
            </div>
            <button type="button" aria-label={`移除${prefix}条件 ${index + 1}`}
              className={`${SECONDARY} col-start-2 row-start-1 justify-center sm:col-auto sm:row-auto`}
              onClick={() => onChange(rules.filter((_, i) => i !== index))}>
              <X aria-hidden="true" className="size-4" />
            </button>
          </div>
        )
      })}
      <button type="button" className={SECONDARY} disabled={!choices.length || rules.length >= (market ? 8 : 16)}
        onClick={() => {
          const first = choices[0]
          if (first) onChange([...rules, {
            table_id: first.table.id, field: first.field.name,
            op: first.field.dtype === 'string' ? '==' : '>=', value: '',
          }])
        }}>
        <Plus aria-hidden="true" className="size-3.5" />添加{prefix}条件
      </button>
      {loading && <p role="status" className="text-xs text-secondary">正在读取扩展时序表…</p>}
      {error && <p role="alert" className="text-xs text-danger">扩展表列表读取失败。可关闭配置后重新打开。</p>}
      {!loading && !error && !choices.length && !rules.length && (
        <p className="text-xs text-secondary">在「数据 → 扩展数据」导入{market ? '市场级' : '按个股记录的'}历史时序表后可添加条件。</p>
      )}
    </div>
  )
}

export function EntryFilterSettings({
  marketSource, onMarketSource, states, onStates, minScore, onMinScore,
  rules, onRules, extensionAvailable, error,
}: {
  marketSource: MarketFilterSource
  onMarketSource: (source: MarketFilterSource) => void
  states: string[]
  onStates: (states: string[]) => void
  minScore: number | ''
  onMinScore: (score: number | '') => void
  rules: EntryFilters
  onRules: (rules: EntryFilters) => void
  extensionAvailable: boolean
  error: string | null
}) {
  const query = useQuery({
    queryKey: QK.extData, queryFn: api.extDataList,
    enabled: extensionAvailable, retry: false,
  })
  const tables = (query.data?.items ?? []).filter(table => table.mode === 'timeseries')
  return (
    <div className="space-y-4">
      <section aria-label="市场级过滤" className="space-y-2.5">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div>
            <h3 className="text-sm font-medium text-foreground">市场级</h3>
            <p className="mt-1 text-xs text-secondary">用上一交易日的市场数据判断当天是否允许开仓。</p>
          </div>
          <label className="w-full text-xs text-secondary sm:w-48">
            <span className="mb-1 block">市场来源</span>
            <select value={marketSource} className={SELECT}
              onChange={event => onMarketSource(event.target.value as MarketFilterSource)}>
              <option value="off">关闭</option>
              <option value="builtin">内置市场环境</option>
              <option value="extension" disabled={!extensionAvailable}>市场级扩展表</option>
            </select>
          </label>
        </div>
        {marketSource === 'builtin' && (
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(REGIME_STATE_LABELS).map(([state, label]) => (
                <button key={state} type="button" aria-pressed={states.includes(state)}
                  className={`${SECONDARY} ${states.includes(state) ? 'border-accent/50 bg-accent/10 text-accent' : ''}`}
                  onClick={() => onStates(states.includes(state) ? states.filter(value => value !== state) : [...states, state])}>
                  {label}
                </button>
              ))}
            </div>
            <label className="w-24 text-xs text-secondary">
              <span className="mb-1 block">最低分</span>
              <input className={SELECT} type="number" min={0} max={100} placeholder="不限" value={minScore}
                onChange={event => onMinScore(event.target.value === '' ? '' : Number(event.target.value))} />
            </label>
          </div>
        )}
        {marketSource === 'extension' && extensionAvailable && (
          <ExtensionConditions market tables={tables.filter(table => table.market_level)} rules={rules.market}
            loading={query.isLoading} error={query.isError} onChange={market => onRules({ ...rules, market })} />
        )}
      </section>
      <section aria-label="个股级过滤" className="space-y-2.5 border-t border-border pt-4">
        <div>
          <h3 className="text-sm font-medium text-foreground">个股级</h3>
          <p className="mt-1 text-xs text-secondary">按标的和交易日筛选，支持历史行业、概念和数值字段。</p>
        </div>
        {extensionAvailable ? (
          <ExtensionConditions tables={tables.filter(table => !table.market_level)} rules={rules.stocks}
            loading={query.isLoading} error={query.isError} onChange={stocks => onRules({ ...rules, stocks })} />
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-xs text-secondary">扩展过滤可用于 A 股日线策略。</p>
            {!!rules.stocks.length && <button type="button" className={SECONDARY}
              onClick={() => onRules({ ...rules, stocks: [] })}>清除个股条件</button>}
          </div>
        )}
      </section>
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </div>
  )
}
