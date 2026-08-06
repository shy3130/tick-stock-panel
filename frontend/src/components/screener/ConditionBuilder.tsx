import { useMemo } from 'react'
import type { ScreenerCondition, ScreenerFieldSpec } from '@/lib/api'

const DEFAULT_NUMERIC_OPS = ['>', '<', '>=', '<=', '=', '!=', 'between', 'in']
const DEFAULT_ENUM_OPS = ['=', '!=', 'in']
const DEFAULT_BOOLEAN_OPS = ['=']
const GROUP_LABELS: Record<string, string> = {
  market: '行情',
  market_cap: '市值',
  technical: '技术',
  limit_up: '涨停',
  financial: '基本面',
  filter: '板块过滤',
}

interface ConditionBuilderProps {
  fields: ScreenerFieldSpec[]
  value: ScreenerCondition[]
  onChange: (conditions: ScreenerCondition[]) => void
}

function opsFor(spec: ScreenerFieldSpec | undefined): string[] {
  if (spec?.ops?.length) return spec.ops
  if (spec?.value_type === 'boolean') return DEFAULT_BOOLEAN_OPS
  if (spec?.value_type === 'enum') return DEFAULT_ENUM_OPS
  return DEFAULT_NUMERIC_OPS
}

function defaultValue(spec: ScreenerFieldSpec | undefined, op: string): ScreenerCondition['value'] {
  if (spec?.value_type === 'boolean') return false
  if (op === 'between' || op === 'in') return []
  if (spec?.value_type === 'numeric') return null
  return ''
}

function firstAvailable(fields: ScreenerFieldSpec[]) {
  return fields.find(field => field.availability === 'available')
}

function numericValue(value: ScreenerCondition['value']): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function listValue(value: ScreenerCondition['value']): Array<number | string> {
  return Array.isArray(value) ? value : []
}

export function isConditionValid(condition: ScreenerCondition, fields: ScreenerFieldSpec[]): boolean {
  const spec = fields.find(field => field.field === condition.field)
  if (!spec || spec.availability !== 'available' || !opsFor(spec).includes(condition.op)) return false

  if (spec.value_type === 'boolean') return typeof condition.value === 'boolean'
  if (spec.value_type === 'numeric') {
    if (condition.op === 'between') {
      const values = listValue(condition.value)
      return values.length === 2
        && values.every(item => typeof item === 'number' && Number.isFinite(item))
        && Number(values[0]) <= Number(values[1])
    }
    if (condition.op === 'in') {
      const values = listValue(condition.value)
      return values.length > 0 && values.length <= 50
        && values.every(item => typeof item === 'number' && Number.isFinite(item))
    }
    return numericValue(condition.value) !== null
  }

  if (condition.op === 'in') {
    const values = listValue(condition.value)
    return values.length > 0 && values.length <= 50
      && values.every(item => typeof item === 'string' && item.trim().length > 0 && item.trim().length <= 64)
  }
  return typeof condition.value === 'string'
    && condition.value.trim().length > 0
    && condition.value.trim().length <= 64
}

export function areConditionsValid(conditions: ScreenerCondition[], fields: ScreenerFieldSpec[]): boolean {
  return conditions.length > 0 && conditions.length <= 20 && conditions.every(condition => isConditionValid(condition, fields))
}

function parseNumberList(text: string): Array<number | string> {
  return text
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => {
      const value = Number(item)
      return Number.isFinite(value) ? value : item
    })
}

function parseTextList(text: string): string[] {
  return text.split(',').map(item => item.trim()).filter(Boolean)
}

export function ConditionBuilder({ fields, value, onChange }: ConditionBuilderProps) {
  const groupedFields = useMemo(() => {
    const groups = new Map<string, ScreenerFieldSpec[]>()
    for (const field of fields) {
      const group = groups.get(field.group)
      if (group) group.push(field)
      else groups.set(field.group, [field])
    }
    return [...groups.entries()]
  }, [fields])

  const update = (index: number, patch: Partial<ScreenerCondition>) => {
    const next = value.slice()
    next[index] = { ...next[index], ...patch }
    onChange(next)
  }

  const add = () => {
    if (value.length >= 20) return
    const spec = firstAvailable(fields)
    if (!spec) return
    const op = opsFor(spec)[0] ?? '='
    onChange([...value, { field: spec.field, op, value: defaultValue(spec, op) }])
  }

  const changeField = (index: number, field: string) => {
    const spec = fields.find(item => item.field === field)
    const op = opsFor(spec)[0] ?? '='
    update(index, { field, op, value: defaultValue(spec, op) })
  }

  const renderValue = (condition: ScreenerCondition, index: number, spec: ScreenerFieldSpec | undefined) => {
    if (!spec) return <span className="text-xs text-muted">字段元数据加载中…</span>
    if (spec.value_type === 'boolean') {
      return (
        <select
          aria-label="条件值"
          value={String(condition.value === true)}
          onChange={event => update(index, { value: event.target.value === 'true' })}
          className="h-8 rounded-input border border-border bg-elevated px-2 text-xs text-foreground"
        >
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      )
    }

    if (condition.op === 'between') {
      const values = listValue(condition.value)
      const first = typeof values[0] === 'number' ? String(values[0]) : ''
      const second = typeof values[1] === 'number' ? String(values[1]) : ''
      return (
        <span className="inline-flex items-center gap-1">
          <input
            aria-label="条件下限"
            type="number"
            value={first}
            onChange={event => update(index, { value: [event.target.value === '' ? '' : Number(event.target.value), values[1] ?? ''] })}
            className="h-8 w-24 rounded-input border border-border bg-elevated px-2 text-xs num"
          />
          <span className="text-xs text-muted">至</span>
          <input
            aria-label="条件上限"
            type="number"
            value={second}
            onChange={event => update(index, { value: [values[0] ?? '', event.target.value === '' ? '' : Number(event.target.value)] })}
            className="h-8 w-24 rounded-input border border-border bg-elevated px-2 text-xs num"
          />
        </span>
      )
    }

    if (condition.op === 'in') {
      if (spec.value_type === 'numeric') {
        return (
          <input
            aria-label="条件值列表"
            type="text"
            inputMode="decimal"
            value={listValue(condition.value).join(',')}
            placeholder="例如 1,2,3"
            onChange={event => update(index, { value: parseNumberList(event.target.value) })}
            className="h-8 w-40 rounded-input border border-border bg-elevated px-2 text-xs"
          />
        )
      }
      if (spec.options?.length) {
        const selected = listValue(condition.value).filter((item): item is string => typeof item === 'string')
        return (
          <select
            multiple
            aria-label="条件选项列表"
            value={selected}
            onChange={event => update(index, { value: [...event.target.selectedOptions].map(option => option.value) })}
            className="min-h-8 rounded-input border border-border bg-elevated px-2 text-xs text-foreground"
          >
            {spec.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        )
      }
      return (
        <input
          aria-label="条件值列表"
          type="text"
          value={listValue(condition.value).join(',')}
          placeholder="多个值用逗号分隔"
          onChange={event => update(index, { value: parseTextList(event.target.value) })}
          className="h-8 w-40 rounded-input border border-border bg-elevated px-2 text-xs"
        />
      )
    }

    if (spec.value_type === 'numeric') {
      return (
        <input
          aria-label="条件值"
          type="number"
          value={typeof condition.value === 'number' ? String(condition.value) : ''}
          onChange={event => update(index, { value: event.target.value === '' ? null : Number(event.target.value) })}
          className="h-8 w-28 rounded-input border border-border bg-elevated px-2 text-xs num"
        />
      )
    }

    if (spec.options?.length) {
      return (
        <select
          aria-label="条件值"
          value={typeof condition.value === 'string' ? condition.value : ''}
          onChange={event => update(index, { value: event.target.value })}
          className="h-8 min-w-32 rounded-input border border-border bg-elevated px-2 text-xs text-foreground"
        >
          <option value="">请选择</option>
          {spec.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      )
    }

    return (
      <input
        aria-label="条件值"
        type="text"
        value={typeof condition.value === 'string' ? condition.value : ''}
        onChange={event => update(index, { value: event.target.value })}
        className="h-8 min-w-32 rounded-input border border-border bg-elevated px-2 text-xs"
      />
    )
  }

  return (
    <div className="space-y-2" aria-label="结构化筛选条件">
      {value.map((condition, index) => {
        const spec = fields.find(field => field.field === condition.field)
        const ops = opsFor(spec)
        return (
          <div key={`${condition.field}-${index}`} className="flex flex-wrap items-center gap-2 rounded-input border border-border/70 bg-surface/60 p-2">
            <label className="sr-only" htmlFor={`condition-field-${index}`}>字段</label>
            <select
              id={`condition-field-${index}`}
              aria-label="筛选字段"
              value={condition.field}
              onChange={event => changeField(index, event.target.value)}
              className="h-8 min-w-40 rounded-input border border-border bg-elevated px-2 text-xs text-foreground"
            >
              {groupedFields.map(([group, groupFields]) => (
                <optgroup key={group} label={GROUP_LABELS[group] ?? group}>
                  {groupFields.map(field => (
                    <option key={field.field} value={field.field} disabled={field.availability === 'unavailable'}>
                      {field.label}{field.availability === 'unavailable' ? `（${field.null_policy || '暂不可用'}）` : ''}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>

            <label className="sr-only" htmlFor={`condition-op-${index}`}>运算符</label>
            <select
              id={`condition-op-${index}`}
              aria-label="筛选运算符"
              value={ops.includes(condition.op) ? condition.op : ops[0] ?? '='}
              onChange={event => update(index, { op: event.target.value, value: defaultValue(spec, event.target.value) })}
              className="h-8 min-w-20 rounded-input border border-border bg-elevated px-2 text-xs text-foreground"
            >
              {ops.map(op => <option key={op} value={op}>{op}</option>)}
            </select>

            {renderValue(condition, index, spec)}
            {spec?.unit && <span className="text-[11px] text-muted">{spec.unit}</span>}
            {spec?.field === 'change_pct' && <span className="text-[11px] text-accent">0.05 = 5%</span>}
            {spec?.availability === 'unavailable' && <span className="text-[11px] text-warning">{spec.null_policy || '当前数据源不可用'}</span>}
            <button
              type="button"
              onClick={() => onChange(value.filter((_, rowIndex) => rowIndex !== index))}
              className="ml-auto h-8 rounded-btn px-2 text-xs text-muted hover:bg-danger/10 hover:text-danger"
            >
              删除
            </button>
          </div>
        )
      })}
      <button
        type="button"
        onClick={add}
        disabled={value.length >= 20 || fields.every(field => field.availability === 'unavailable')}
        className="h-8 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40"
      >
        + 添加条件 {value.length >= 20 ? '（最多 20 条）' : ''}
      </button>
    </div>
  )
}
