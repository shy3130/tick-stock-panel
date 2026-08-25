import type { ScreenerCondition, ScreenerFieldSpec } from '@/lib/api'

const DEFAULT_NUMERIC_OPS = ['>', '<', '>=', '<=', '=', '!=', 'between', 'in']
const DEFAULT_ENUM_OPS = ['=', '!=', 'in']
const DEFAULT_BOOLEAN_OPS = ['=']

export const GROUP_LABELS: Record<string, string> = {
  market: '行情',
  market_cap: '市值',
  technical: '技术',
  limit_up: '涨停',
  financial: '基本面',
  filter: '板块过滤',
  reference: '标的属性',
  lhb: '龙虎榜',
  chip: '筹码',
  moneyflow: '资金流',
  margin: '融资融券',
  // 后端 sequence metadata 已用中文 group「多日形态」；此处仅作同义键兜底
  sequence: '多日形态',
  '多日形态': '多日形态',
}

export function opsFor(spec: ScreenerFieldSpec | undefined): string[] {
  if (spec?.ops?.length) return spec.ops
  if (spec?.value_type === 'boolean') return DEFAULT_BOOLEAN_OPS
  if (spec?.value_type === 'enum') return DEFAULT_ENUM_OPS
  return DEFAULT_NUMERIC_OPS
}

export function defaultValue(spec: ScreenerFieldSpec | undefined, op: string): ScreenerCondition['value'] {
  if (spec?.value_type === 'boolean') return false
  if (op === 'between' || op === 'in') return []
  if (spec?.value_type === 'numeric') return null
  return ''
}

export function numericValue(value: ScreenerCondition['value']): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function listValue(value: ScreenerCondition['value']): Array<number | string> {
  return Array.isArray(value) ? value : []
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

interface ConditionValueEditorProps {
  spec: ScreenerFieldSpec | undefined
  op: string
  value: ScreenerCondition['value']
  onChange: (value: ScreenerCondition['value']) => void
}

/** 单条条件的取值编辑器：布尔 / 区间 / 列表 / 数值 / 枚举 / 文本。 */
export function ConditionValueEditor({ spec, op, value, onChange }: ConditionValueEditorProps) {
  if (!spec) return <span className="text-xs text-muted">字段元数据加载中…</span>

  if (spec.value_type === 'boolean') {
    return (
      <select
        aria-label="条件值"
        value={String(value === true)}
        onChange={event => onChange(event.target.value === 'true')}
        className="control h-9 w-full px-2 text-xs"
      >
        <option value="true">是</option>
        <option value="false">否</option>
      </select>
    )
  }

  if (op === 'between') {
    const values = listValue(value)
    const first = typeof values[0] === 'number' ? String(values[0]) : ''
    const second = typeof values[1] === 'number' ? String(values[1]) : ''
    return (
      <span className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-1.5">
        <input
          aria-label="条件下限"
          type="number"
          value={first}
          onChange={event => onChange([event.target.value === '' ? '' : Number(event.target.value), values[1] ?? ''])}
          className="control h-9 min-w-0 px-2 text-xs num"
        />
        <span className="text-xs text-muted">至</span>
        <input
          aria-label="条件上限"
          type="number"
          value={second}
          onChange={event => onChange([values[0] ?? '', event.target.value === '' ? '' : Number(event.target.value)])}
          className="control h-9 min-w-0 px-2 text-xs num"
        />
      </span>
    )
  }

  if (op === 'in') {
    if (spec.value_type === 'numeric') {
      return (
        <input
          aria-label="条件值列表"
          type="text"
          inputMode="decimal"
          value={listValue(value).join(',')}
          placeholder="例如 1,2,3"
          onChange={event => onChange(parseNumberList(event.target.value))}
          className="control h-9 w-full px-2 text-xs"
        />
      )
    }
    if (spec.options?.length) {
      const selected = listValue(value).filter((item): item is string => typeof item === 'string')
      return (
        <select
          multiple
          size={Math.min(4, spec.options.length)}
          aria-label="条件选项列表"
          value={selected}
          onChange={event => onChange([...event.target.selectedOptions].map(option => option.value))}
          className="control min-h-20 w-full px-2 text-xs"
        >
          {spec.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      )
    }
    return (
      <input
        aria-label="条件值列表"
        type="text"
        value={listValue(value).join(',')}
        placeholder="多个值用逗号分隔"
        onChange={event => onChange(parseTextList(event.target.value))}
        className="control h-9 w-full px-2 text-xs"
      />
    )
  }

  if (spec.value_type === 'numeric') {
    return (
      <input
        aria-label="条件值"
        type="number"
        value={typeof value === 'number' ? String(value) : ''}
        onChange={event => onChange(event.target.value === '' ? null : Number(event.target.value))}
        className="control h-9 w-full px-2 text-xs num"
      />
    )
  }

  if (spec.options?.length) {
    return (
      <select
        aria-label="条件值"
        value={typeof value === 'string' ? value : ''}
        onChange={event => onChange(event.target.value)}
        className="control h-9 min-w-0 w-full px-2 text-xs"
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
      value={typeof value === 'string' ? value : ''}
      onChange={event => onChange(event.target.value)}
      className="control h-9 min-w-0 w-full px-2 text-xs"
    />
  )
}
