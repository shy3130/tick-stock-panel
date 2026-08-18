import { useMemo } from 'react'
import { Plus } from 'lucide-react'
import type { ScreenerCondition, ScreenerFieldSpec } from '@/lib/api'
import { ConditionValueEditor, GROUP_LABELS, defaultValue, listValue, numericValue, opsFor } from './ConditionValueEditor'

interface ConditionBuilderProps {
  fields: ScreenerFieldSpec[]
  value: ScreenerCondition[]
  onChange: (conditions: ScreenerCondition[]) => void
}

function firstAvailable(fields: ScreenerFieldSpec[]) {
  return fields.find(field => field.availability === 'available')
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


  return (
    <div className="space-y-2" aria-label="结构化筛选条件">
      {value.length === 0 && (
        <div className="flex flex-col gap-1 rounded-input border border-dashed border-border/80 bg-elevated/35 px-3 py-3 text-xs sm:flex-row sm:items-center sm:justify-between">
          <span className="text-secondary">从预设填入，或添加字段、运算符和值。所有条件按“且”组合。</span>
          <span className="num text-[11px] text-muted">最多 20 条</span>
        </div>
      )}
      {value.map((condition, index) => {
        const spec = fields.find(field => field.field === condition.field)
        const ops = opsFor(spec)
        const invalid = !isConditionValid(condition, fields)
        return (
          <div
            key={`${condition.field}-${index}`}
            data-invalid={invalid || undefined}
            className={`group grid min-w-0 grid-cols-1 gap-2 rounded-input border p-2 transition-colors sm:grid-cols-[auto_minmax(10rem,1.45fr)_minmax(4.75rem,0.5fr)_minmax(9rem,0.9fr)_auto_auto] sm:items-center ${
              invalid
                ? 'border-warning/45 bg-warning/5'
                : 'border-border/70 bg-surface/60 hover:border-accent/45 hover:bg-elevated/40'
            }`}
          >
            <span
              aria-hidden="true"
              className="inline-flex h-8 w-8 items-center justify-center rounded-input border border-border bg-elevated font-mono text-[10px] text-muted sm:h-9 sm:w-9"
            >
              {String(index + 1).padStart(2, '0')}
            </span>

            <label className="sr-only" htmlFor={`condition-field-${index}`}>字段</label>
            <select
              id={`condition-field-${index}`}
              aria-label="筛选字段"
              aria-invalid={invalid || undefined}
              value={condition.field}
              onChange={event => changeField(index, event.target.value)}
              className="control h-9 min-w-0 w-full px-2 text-xs"
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
              aria-invalid={invalid || undefined}
              value={ops.includes(condition.op) ? condition.op : ops[0] ?? '='}
              onChange={event => update(index, { op: event.target.value, value: defaultValue(spec, event.target.value) })}
              className="control h-9 min-w-0 w-full px-2 text-xs"
            >
              {ops.map(op => <option key={op} value={op}>{op}</option>)}
            </select>

            <ConditionValueEditor
              spec={spec}
              op={condition.op}
              value={condition.value}
              onChange={next => update(index, { value: next })}
            />
            <div className="flex min-h-5 min-w-0 items-center text-[11px] text-muted">
              {spec?.unit && <span>{spec.unit}</span>}
              {spec?.field === 'change_pct' && <span className="text-accent">0.05 = 5%</span>}
              {spec?.availability === 'unavailable' && <span className="text-warning">{spec.null_policy || '当前数据源不可用'}</span>}
            </div>
            <button
              type="button"
              onClick={() => onChange(value.filter((_, rowIndex) => rowIndex !== index))}
              aria-label={`删除第 ${index + 1} 条条件`}
              className="btn-ghost h-9 justify-self-start px-2 text-xs text-muted hover:bg-danger/10 hover:text-danger sm:justify-self-end"
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
        className="btn-secondary h-9 px-3 text-xs"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
        添加条件 {value.length >= 20 ? '（最多 20 条）' : ''}
      </button>
    </div>
  )
}
