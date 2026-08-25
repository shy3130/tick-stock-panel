import { useMemo, useState } from 'react'
import { Search, Trash2 } from 'lucide-react'
import { SCREENER_CONDITION_GROUPS, type ScreenerCondition, type ScreenerFieldSpec } from '@/lib/api'
import { latestOnlyBadgeLabel, normalizeConditionGroup } from '@/lib/screenerResult'
import { isConditionValid } from './ConditionBuilder'
import { ConditionValueEditor, GROUP_LABELS, defaultValue, opsFor } from './ConditionValueEditor'

const MAX_CONDITIONS = 20

interface AdvancedFilterPanelProps {
  fields: ScreenerFieldSpec[]
  value: ScreenerCondition[]
  onChange: (conditions: ScreenerCondition[]) => void
  /** F8: 历史日期下禁用「仅最新日」字段（最新日查询时传 undefined） */
  isFieldDisabled?: (spec: ScreenerFieldSpec) => boolean
}
/**
 * 高级筛选面板：按分组平铺全部字段，勾选即启用该条件，未勾选的字段不参与筛选。
 * 与逐条添加模式共享同一份 conditions 状态。
 */
export function AdvancedFilterPanel({ fields, value, onChange, isFieldDisabled }: AdvancedFilterPanelProps) {
  const [search, setSearch] = useState('')
  const [selectedOnly, setSelectedOnly] = useState(false)

  const firstIndexByField = useMemo(() => {
    const map = new Map<string, number>()
    value.forEach((condition, index) => {
      if (!map.has(condition.field)) map.set(condition.field, index)
    })
    return map
  }, [value])

  const countByField = useMemo(() => {
    const map = new Map<string, number>()
    for (const condition of value) map.set(condition.field, (map.get(condition.field) ?? 0) + 1)
    return map
  }, [value])

  const grouped = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    const groups = new Map<string, ScreenerFieldSpec[]>()
    for (const field of fields) {
      if (selectedOnly && !firstIndexByField.has(field.field)) continue
      if (keyword) {
        const haystack = `${field.field} ${field.label} ${GROUP_LABELS[field.group] ?? field.group}`.toLowerCase()
        if (!haystack.includes(keyword)) continue
      }
      const group = groups.get(field.group)
      if (group) group.push(field)
      else groups.set(field.group, [field])
    }
    return [...groups.entries()]
  }, [fields, search, selectedOnly, firstIndexByField])

  const toggleField = (spec: ScreenerFieldSpec, enabled: boolean) => {
    if (enabled) {
      if (value.length >= MAX_CONDITIONS) return
      const op = opsFor(spec)[0] ?? '='
      const initial = spec.value_type === 'boolean' ? true : defaultValue(spec, op)
      onChange([...value, { field: spec.field, op, value: initial, group: 'A' }])
      return
    }
    onChange(value.filter(condition => condition.field !== spec.field))
  }

  const updateFirst = (field: string, patch: Partial<ScreenerCondition>) => {
    const index = firstIndexByField.get(field)
    if (index === undefined) return
    const next = value.slice()
    next[index] = { ...next[index], ...patch }
    onChange(next)
  }

  const changeOp = (field: string, spec: ScreenerFieldSpec, op: string) => {
    updateFirst(field, { op, value: defaultValue(spec, op) })
  }

  const atCapacity = value.length >= MAX_CONDITIONS

  return (
    <div className="space-y-3" aria-label="高级筛选面板">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-44 flex-1 sm:max-w-72">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden="true" />
          <label className="sr-only" htmlFor="advanced-filter-search">搜索条件字段</label>
          <input
            id="advanced-filter-search"
            type="search"
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder="搜索字段 / 名称 / 分组"
            className="control h-9 w-full pl-8 text-xs"
          />
        </div>
        <label className="flex cursor-pointer items-center gap-1.5 text-xs text-secondary">
          <input
            type="checkbox"
            checked={selectedOnly}
            onChange={event => setSelectedOnly(event.target.checked)}
            className="h-3.5 w-3.5 rounded border-border accent-accent"
          />
          只看已选
        </label>
        <span className="text-[11px] text-muted num">已选 {value.length}/{MAX_CONDITIONS}</span>
        {value.length > 0 && (
          <button
            type="button"
            onClick={() => onChange([])}
            className="btn-ghost h-9 px-2 text-xs text-muted hover:bg-danger/10 hover:text-danger"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            清空全部
          </button>
        )}
      </div>
      <p className="text-[11px] text-muted">勾选即启用该条件；同一组内按“且”组合。多组时由上方逻辑开关决定组间关系。</p>

      {grouped.map(([group, groupFields]) => {
        const selectedInGroup = groupFields.filter(field => firstIndexByField.has(field.field)).length
        return (
          <div key={group} className="space-y-1.5">
            <div className="flex items-center gap-2">
              <span className="section-kicker">{GROUP_LABELS[group] ?? group}</span>
              <span className="text-[10px] text-muted num">{selectedInGroup}/{groupFields.length}</span>
              <span className="h-px flex-1 bg-border" aria-hidden="true" />
            </div>
            <div className="overflow-hidden rounded-input border border-border/70">
              {groupFields.map((spec, rowIndex) => {
                const firstIndex = firstIndexByField.get(spec.field)
                const checked = firstIndex !== undefined
                const duplicates = (countByField.get(spec.field) ?? 0) - 1
                const unavailable = spec.availability === 'unavailable'
                const condition = checked ? value[firstIndex] : undefined
                const invalid = checked && condition ? !isConditionValid(condition, fields) : false
                const ops = opsFor(spec)
                return (
                  <div
                    key={spec.field}
                    data-invalid={invalid || undefined}
                    className={`flex flex-col gap-2 px-2.5 py-2 sm:grid sm:grid-cols-[minmax(9rem,1fr)_auto_minmax(4.5rem,auto)_minmax(11rem,1.25fr)] sm:items-center ${rowIndex > 0 ? 'border-t border-border/50' : ''} ${invalid ? 'bg-warning/5' : checked ? 'bg-elevated/40' : ''}`}
                  >
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={unavailable || (!checked && (isFieldDisabled?.(spec) || atCapacity))}
                        onChange={event => toggleField(spec, event.target.checked)}
                        aria-label={`启用条件 ${spec.label}`}
                        className="h-3.5 w-3.5 shrink-0 rounded border-border accent-accent"
                      />
                      <span
                        className={`min-w-0 truncate text-xs ${unavailable ? 'text-muted' : checked ? 'font-medium text-foreground' : 'text-secondary'}`}
                        title={spec.field}
                      >
                        {spec.label}
                        {spec.unit ? <span className="ml-1 text-[10px] font-normal text-muted">({spec.unit})</span> : null}
                        {latestOnlyBadgeLabel(spec) && (
                          <span
                            className="ml-1.5 rounded-full border border-border bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-secondary"
                            title="该字段仅有最新交易日数据，历史日期查询不可用"
                          >
                            {latestOnlyBadgeLabel(spec)}
                          </span>
                        )}
                      </span>
                      {duplicates > 0 && (
                        <span
                          className="rounded-full bg-accent/10 px-1.5 text-[10px] text-accent num"
                          title="该字段有多条条件（来自预设或自然语言解析），取消勾选将全部移除"
                        >
                          +{duplicates}
                        </span>
                      )}
                      {spec.field === 'change_pct' && <span className="text-[10px] text-accent">0.05 = 5%</span>}
                      {unavailable && <span className="text-[10px] text-warning">{spec.null_policy || '当前数据源不可用'}</span>}
                    </div>
                    {checked && condition ? (
                      <>
                        <label className="sr-only" htmlFor={`advanced-group-${spec.field}`}>{spec.label} 分组</label>
                        <select
                          id={`advanced-group-${spec.field}`}
                          aria-label={`${spec.label} 条件分组`}
                          value={normalizeConditionGroup(condition.group)}
                          onChange={event => updateFirst(spec.field, { group: event.target.value })}
                          title="条件分组 A-E"
                          className="control h-9 w-full min-w-[3.25rem] px-1.5 text-center font-mono text-xs font-semibold sm:w-12"
                        >
                          {SCREENER_CONDITION_GROUPS.map(group => (
                            <option key={group} value={group}>{group}</option>
                          ))}
                        </select>
                        <label className="sr-only" htmlFor={`advanced-op-${spec.field}`}>{spec.label} 运算符</label>
                        <select
                          id={`advanced-op-${spec.field}`}
                          aria-label="筛选运算符"
                          aria-invalid={invalid || undefined}
                          value={ops.includes(condition.op) ? condition.op : ops[0] ?? '='}
                          onChange={event => changeOp(spec.field, spec, event.target.value)}
                          className="control h-9 w-full px-2 text-xs"
                        >
                          {ops.map(op => <option key={op} value={op}>{op}</option>)}
                        </select>
                        <ConditionValueEditor
                          spec={spec}
                          op={condition.op}
                          value={condition.value}
                          onChange={next => updateFirst(spec.field, { value: next })}
                        />
                      </>
                    ) : (
                      <span className="text-[11px] text-muted sm:col-span-3">未选择</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}
      {grouped.length === 0 && (
        <div className="rounded-input border border-dashed border-border/80 bg-elevated/35 px-3 py-6 text-center text-xs text-muted">
          没有匹配的字段。
        </div>
      )}
    </div>
  )
}
