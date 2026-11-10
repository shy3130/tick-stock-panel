import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Pencil, Plus, Save, Trash2, X } from 'lucide-react'
import { api, type FactorColumn, type ScoringDirection } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { definitionOptionLabel } from '@/lib/definitionOptions'

interface Props {
  value: Record<string, number>
  directions: Record<string, ScoringDirection>
  onChange: (value: Record<string, number>, directions: Record<string, ScoringDirection>) => void
  fallbackLabels?: Record<string, string>
}

function weightsToPercentages(values: Record<string, number>) {
  const entries = Object.entries(values).map(([name, value]) => [
    name,
    Math.max(0, Number(value) || 0),
  ] as const)
  const total = entries.reduce((sum, [, value]) => sum + value, 0)
  if (total <= 0) {
    return Object.fromEntries(entries.map(([name]) => [name, 0])) as Record<string, number>
  }

  const shares = entries.map(([name, value], index) => {
    const exact = value / total * 100
    return { name, index, value: Math.floor(exact), remainder: exact - Math.floor(exact) }
  })
  let remaining = 100 - shares.reduce((sum, item) => sum + item.value, 0)
  for (const item of [...shares].sort((a, b) => b.remainder - a.remainder || a.index - b.index)) {
    if (remaining <= 0) break
    item.value += 1
    remaining -= 1
  }
  return Object.fromEntries(shares.map(item => [item.name, item.value])) as Record<string, number>
}

function normalizePercentages(values: Record<string, number>) {
  const active = Object.entries(values).filter(([, value]) => Number(value) > 0)
  const total = active.reduce((sum, [, value]) => sum + Number(value), 0)
  if (total <= 0) return {}
  return Object.fromEntries(
    active.map(([name, value]) => [name, +(Number(value) / total).toFixed(6)]),
  ) as Record<string, number>
}

function ScoringRow({ name, label, weight, direction, editing, onWeightChange, onDirectionChange, onRemove }: {
  name: string
  label: string
  weight: number
  direction: ScoringDirection
  editing: boolean
  onWeightChange: (value: number) => void
  onDirectionChange: (value: ScoringDirection) => void
  onRemove: () => void
}) {
  return (
    <div className="grid min-h-8 grid-cols-[minmax(4rem,6.5rem)_3.75rem_minmax(3.5rem,1fr)_2.25rem_1.75rem] items-center gap-1.5">
      <span className="truncate text-right text-[11px] text-secondary" title={`${label} · ${name}`}>{label}</span>
      {editing ? (
        <div className="grid h-6 grid-cols-2 overflow-hidden rounded border border-border bg-base">
          {([['high', ArrowUp, '偏好高值'], ['low', ArrowDown, '偏好低值']] as const).map(([value, Icon, title]) => (
            <button
              key={value}
              type="button"
              onClick={() => onDirectionChange(value)}
              className={`flex items-center justify-center transition-colors ${direction === value
                ? value === 'high' ? 'bg-emerald-400/15 text-emerald-400' : 'bg-cyan-400/15 text-cyan-400'
                : 'text-muted hover:bg-elevated hover:text-secondary'
              }`}
              title={title}
              aria-label={`${label}${title}`}
              aria-pressed={direction === value}
            >
              <Icon className="h-3 w-3" />
            </button>
          ))}
        </div>
      ) : (
        <span className={`flex items-center justify-center gap-1 text-[10px] ${direction === 'low' ? 'text-cyan-400' : 'text-emerald-400'}`}>
          {direction === 'low' ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />}
          {direction === 'low' ? '低值' : '高值'}
        </span>
      )}
      {editing ? (
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={weight}
          onChange={event => onWeightChange(Number(event.target.value))}
          className="h-1 min-w-0 cursor-pointer accent-amber-400"
          aria-label={`${label}权重`}
        />
      ) : (
        <div className="h-1.5 min-w-0 overflow-hidden rounded-full bg-elevated">
          <div className="h-full rounded-full bg-amber-400/70" style={{ width: `${Math.min(weight, 100)}%` }} />
        </div>
      )}
      <span className="text-right font-mono text-[10px] text-muted">{weight}%</span>
      {editing ? (
        <button
          type="button"
          onClick={onRemove}
          className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-danger/10 hover:text-danger"
          title={`移除${label}`}
          aria-label={`移除评分因子${label}`}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      ) : <span aria-hidden="true" />}
    </div>
  )
}

export function ScoringEditor({ value, directions, onChange, fallbackLabels = {} }: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, number>>(() => weightsToPercentages(value))
  const [directionDraft, setDirectionDraft] = useState<Record<string, ScoringDirection>>(directions)
  const [factorToAdd, setFactorToAdd] = useState('')
  const factors = useQuery({
    queryKey: QK.factorColumns,
    queryFn: api.factorColumns,
    staleTime: 5 * 60_000,
  })
  const factorLabels = useMemo(() => Object.fromEntries(
    (factors.data?.columns ?? []).map(item => [item.id, definitionOptionLabel(item)]),
  ), [factors.data])
  const factorGroups = useMemo(() => {
    const groups: Record<string, FactorColumn[]> = {}
    for (const item of factors.data?.columns ?? []) {
      ;(groups[item.group] ??= []).push(item)
    }
    return groups
  }, [factors.data])

  useEffect(() => {
    if (editing) return
    setDraft(weightsToPercentages(value))
    setDirectionDraft(directions)
  }, [directions, editing, value])

  const startEditing = () => {
    setDraft(weightsToPercentages(value))
    setDirectionDraft(directions)
    setFactorToAdd('')
    setEditing(true)
  }
  const cancelEditing = () => {
    setDraft(weightsToPercentages(value))
    setDirectionDraft(directions)
    setFactorToAdd('')
    setEditing(false)
  }
  const saveDraft = () => {
    const normalized = normalizePercentages(draft)
    const nextDirections = Object.fromEntries(
      Object.keys(normalized).map(name => [name, directionDraft[name] ?? 'high']),
    ) as Record<string, ScoringDirection>
    onChange(normalized, nextDirections)
    setFactorToAdd('')
    setEditing(false)
  }
  const addFactor = () => {
    if (!factorToAdd || factorToAdd in draft) return
    setDraft(current => ({ ...current, [factorToAdd]: Object.keys(current).length > 0 ? 10 : 100 }))
    setDirectionDraft(current => ({ ...current, [factorToAdd]: 'high' }))
    setFactorToAdd('')
  }
  const removeFactor = (name: string) => {
    setDraft(current => Object.fromEntries(
      Object.entries(current).filter(([key]) => key !== name),
    ))
    setDirectionDraft(current => Object.fromEntries(
      Object.entries(current).filter(([key]) => key !== name),
    ) as Record<string, ScoringDirection>)
  }

  const visibleWeights = editing ? draft : weightsToPercentages(value)
  const visibleDirections = editing ? directionDraft : directions
  const visibleKeys = Object.keys(visibleWeights)
  const draftTotal = Object.values(visibleWeights).reduce((sum, weight) => sum + weight, 0)

  return (
    <div className="space-y-3">
      {editing && (
        <div className="flex gap-2 border-b border-border/40 pb-3">
          <select
            value={factorToAdd}
            onChange={event => setFactorToAdd(event.target.value)}
            disabled={factors.isLoading || factors.isError}
            className="h-8 min-w-0 flex-1 rounded-input border border-border bg-base px-2 text-xs text-secondary focus:border-accent focus:outline-none disabled:opacity-50"
            aria-label="选择要添加的评分因子"
          >
            <option value="">
              {factors.isLoading ? '加载因子目录…' : factors.isError ? '因子目录加载失败' : '选择评分因子'}
            </option>
            {Object.entries(factorGroups).map(([group, items]) => {
              const available = items.filter(item => !item.archived && !(item.id in draft))
              return available.length > 0 ? (
                <optgroup key={group} label={group}>
                  {available.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
                </optgroup>
              ) : null
            })}
          </select>
          <button
            type="button"
            onClick={addFactor}
            disabled={!factorToAdd}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn border border-accent/30 bg-accent/10 text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-40"
            title="添加评分因子"
            aria-label="添加评分因子"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {visibleKeys.length > 0 ? (
        <div className="space-y-2">
          {visibleKeys.map(name => (
            <ScoringRow
              key={name}
              name={name}
              label={factorLabels[name] ?? fallbackLabels[name] ?? name}
              weight={visibleWeights[name] ?? 0}
              direction={visibleDirections[name] ?? 'high'}
              editing={editing}
              onWeightChange={weight => setDraft(current => ({ ...current, [name]: Math.max(0, weight) }))}
              onDirectionChange={direction => setDirectionDraft(current => ({ ...current, [name]: direction }))}
              onRemove={() => removeFactor(name)}
            />
          ))}
        </div>
      ) : (
        <div className="border-y border-border/40 py-5 text-center text-xs text-muted">
          {editing ? '请选择评分因子' : '当前策略不使用因子评分'}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/40 pt-2">
        <div className="text-[10px] text-muted">
          权重 <span className={`font-mono text-xs font-medium ${editing && draftTotal !== 100 ? 'text-amber-400' : 'text-emerald-400'}`}>
            {editing ? draftTotal : visibleKeys.length > 0 ? 100 : 0}%
          </span>
        </div>
        <div className="flex items-center gap-1">
          {editing && (
            <button
              type="button"
              onClick={cancelEditing}
              className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground"
              title="取消编辑"
              aria-label="取消编辑评分方案"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={editing ? saveDraft : startEditing}
            className="inline-flex h-7 items-center gap-1.5 rounded-btn border border-amber-400/40 bg-amber-400/10 px-2.5 text-[11px] text-amber-400 transition-colors hover:bg-amber-400/15"
          >
            {editing ? <Save className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
            {editing ? '保存方案' : '编辑方案'}
          </button>
        </div>
      </div>
    </div>
  )
}
