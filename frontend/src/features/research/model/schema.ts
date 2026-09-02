import { asArray, asBoolean, asNumber, asRecord, asString, asStringArray } from './parse'

export const WIDGET_KINDS = [
  'symbol_list',
  'date',
  'number',
  'integer',
  'boolean',
  'enum',
  'multi_enum',
] as const
export type WidgetKind = (typeof WIDGET_KINDS)[number]

export interface JsonSchemaNode {
  type?: string | string[]
  format?: string
  enum?: unknown[]
  items?: JsonSchemaNode
  properties?: Record<string, JsonSchemaNode>
  required?: string[]
  default?: unknown
  title?: string
  description?: string
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number | boolean
  exclusiveMaximum?: number | boolean
  minItems?: number
  maxItems?: number
  pattern?: string
  anyOf?: JsonSchemaNode[]
  oneOf?: JsonSchemaNode[]
  allOf?: JsonSchemaNode[]
  $ref?: string
  $defs?: Record<string, JsonSchemaNode>
  definitions?: Record<string, JsonSchemaNode>
  extra?: Record<string, unknown>
  [key: string]: unknown
}

export interface ParameterField {
  name: string
  title: string
  description: string | null
  widget: WidgetKind
  required: boolean
  group: string | null
  enumValues: string[]
  minimum: number | null
  maximum: number | null
  minItems: number | null
  maxItems: number | null
  multiple: boolean
  defaultValue: unknown
}

export interface ParameterFormModel {
  fields: ParameterField[]
  skipped: { name: string; reason: string; required: boolean }[]
  groups: { id: string; title: string; fields: string[] }[]
}

const SCOPE_OWNED_FIELDS = new Set(['symbols', 'symbol_list', 'scope', 'scope_type'])

export function parseJsonSchema(value: unknown): JsonSchemaNode | null {
  return asRecord(value) as JsonSchemaNode | null
}

export function buildParameterForm(
  schema: JsonSchemaNode | null,
  groupHints?: { id: string; title: string; fields: string[] }[],
): ParameterFormModel {
  if (!schema) {
    return { fields: [], skipped: [], groups: groupHints ?? [] }
  }
  const root = resolveNode(schema, schema)
  const properties = root.properties ?? {}
  const required = new Set(root.required ?? [])
  const fields: ParameterField[] = []
  const skipped: ParameterFormModel['skipped'] = []

  for (const [name, raw] of Object.entries(properties)) {
    if (SCOPE_OWNED_FIELDS.has(name)) continue
    const node = resolveNode(raw, schema)
    const widget = resolveWidget(name, node)
    if (!widget) {
      skipped.push({
        name,
        required: required.has(name),
        reason: required.has(name)
          ? '必填字段没有对应的七种控件，无法在工作台编辑'
          : '非七种控件字段，将使用服务端默认值',
      })
      continue
    }
    const enumValues = widget === 'enum' || widget === 'multi_enum' ? collectEnum(node) : []
    fields.push({
      name,
      title: asString(node.title) ?? humanize(name),
      description: asString(node.description),
      widget,
      required: required.has(name),
      group: readGroup(name, node, groupHints),
      enumValues,
      minimum: numericBound(node, 'min'),
      maximum: numericBound(node, 'max'),
      minItems: asNumber(node.minItems),
      maxItems: asNumber(node.maxItems),
      multiple: widget === 'multi_enum' || (widget === 'symbol_list' && primaryType(unwrapComposable(node)) === 'array'),
      defaultValue: node.default,
    })
  }

  const groups = groupHints?.length
    ? groupHints
    : deriveGroups(fields)

  return { fields, skipped, groups }
}

export function defaultParameters(model: ParameterFormModel): Record<string, unknown> {
  const next: Record<string, unknown> = {}
  for (const field of model.fields) {
    if (field.defaultValue !== undefined) {
      next[field.name] = cloneValue(field.defaultValue)
      continue
    }
    if (field.widget === 'boolean') next[field.name] = false
    else if (field.widget === 'multi_enum' || (field.widget === 'symbol_list' && field.multiple)) next[field.name] = []
    else if (field.widget === 'symbol_list') next[field.name] = ''
  }
  return next
}

export function structurallyValid(
  model: ParameterFormModel,
  parameters: Record<string, unknown>,
  scope: { type: string; symbols?: string[] },
): string | null {
  if (scope.type === 'symbols' && !(scope.symbols && scope.symbols.length > 0)) {
    return '请至少添加一个标的，或改用全市场范围。'
  }
  for (const field of model.fields) {
    const value = parameters[field.name]
    if (field.required && isEmptyValue(value)) return `请填写「${field.title}」`
    if (field.widget === 'integer' && value != null && value !== '' && !Number.isInteger(Number(value))) {
      return `「${field.title}」必须是整数`
    }
    if ((field.widget === 'enum' || field.widget === 'multi_enum') && field.enumValues.length > 0 && value != null) {
      if (field.widget === 'enum' && typeof value === 'string' && !field.enumValues.includes(value)) {
        return `「${field.title}」不在允许取值内`
      }
      if (field.widget === 'multi_enum') {
        const selected = asStringArray(value)
        if (selected.some((item) => !field.enumValues.includes(item))) {
          return `「${field.title}」包含未允许的取值`
        }
      }
    }
    if (field.widget === 'symbol_list' && field.maxItems != null && asStringArray(value).length > field.maxItems) {
      return `「${field.title}」最多 ${field.maxItems} 项`
    }
  }
  return null
}

function resolveWidget(name: string, node: JsonSchemaNode): WidgetKind | null {
  const explicit = asString(node['x-ui-widget'] ?? node['x_ui_widget'])
  if (explicit && (WIDGET_KINDS as readonly string[]).includes(explicit)) return explicit as WidgetKind

  const unwrapped = unwrapComposable(node)
  const enums = collectEnum(unwrapped)
  const type = primaryType(unwrapped)

  if (type === 'array') {
    const items = unwrapped.items ? unwrapComposable(unwrapped.items) : {}
    const itemEnum = collectEnum(items)
    if (itemEnum.length > 0) return 'multi_enum'
    if (isSymbolArray(name, items, unwrapped)) return 'symbol_list'
    return null
  }
  if (enums.length > 0 && type !== 'boolean') return 'enum'
  if (type === 'boolean') return 'boolean'
  if (type === 'integer') return 'integer'
  if (type === 'number') return 'number'
  if (isDateField(name, unwrapped)) return 'date'
  if (isSingleSymbol(name, unwrapped)) return 'symbol_list'
  return null
}

function isDateField(name: string, node: JsonSchemaNode): boolean {
  if (node.format === 'date' || node.format === 'date-time') return true
  return /(^|_)(date|start|end|oos_start|signal_date|as_of)(_|$)/i.test(name)
}

function isSingleSymbol(name: string, node: JsonSchemaNode): boolean {
  if (/(symbol)$/i.test(name) || name === 'benchmark_symbol') return true
  const pattern = asString(node.pattern)
  return Boolean(pattern && pattern.includes('\\d{6}'))
}

function isSymbolArray(name: string, items: JsonSchemaNode, parent: JsonSchemaNode): boolean {
  if (name === 'symbols' || name === 'symbol_list' || /symbols$/i.test(name)) return true
  if (asString(parent.format) === 'symbol' || asString(items.format) === 'symbol') return true
  const pattern = asString(items.pattern)
  return Boolean(pattern && pattern.includes('\\d{6}'))
}

function collectEnum(node: JsonSchemaNode): string[] {
  if (Array.isArray(node.enum)) return node.enum.map((item) => String(item))
  const unwrapped = unwrapComposable(node)
  if (unwrapped !== node && Array.isArray(unwrapped.enum)) return unwrapped.enum.map((item) => String(item))
  const items = node.items ? unwrapComposable(node.items) : null
  if (items && Array.isArray(items.enum)) return items.enum.map((item) => String(item))
  return []
}

function unwrapComposable(node: JsonSchemaNode): JsonSchemaNode {
  const options = [...asArray(node.anyOf), ...asArray(node.oneOf)]
    .map((item) => asRecord(item) as JsonSchemaNode | null)
    .filter((item): item is JsonSchemaNode => item !== null)
    .filter((item) => item.type !== 'null')
  if (options.length === 1) return options[0]
  const allOf = asArray(node.allOf)
    .map((item) => asRecord(item) as JsonSchemaNode | null)
    .filter((item): item is JsonSchemaNode => item !== null)
  if (allOf.length === 1) return { ...node, ...allOf[0] }
  return node
}

function resolveNode(node: JsonSchemaNode, root: JsonSchemaNode): JsonSchemaNode {
  if (!node.$ref) return unwrapComposable(node)
  const resolved = resolveRef(node.$ref, root)
  return unwrapComposable({ ...resolved, ...omitRef(node) })
}

function resolveRef(ref: string, root: JsonSchemaNode): JsonSchemaNode {
  const path = ref.replace(/^#\//, '').split('/')
  let cursor: unknown = root
  for (const part of path) {
    if (part === '$defs' || part === 'definitions') {
      cursor = asRecord(cursor)?.[part] ?? asRecord(root)?.[part]
      continue
    }
    cursor = asRecord(cursor)?.[decodeURIComponent(part)]
  }
  return (asRecord(cursor) as JsonSchemaNode | null) ?? {}
}

function omitRef(node: JsonSchemaNode): JsonSchemaNode {
  const { $ref: _ref, ...rest } = node
  return rest
}

function primaryType(node: JsonSchemaNode): string | null {
  if (typeof node.type === 'string') return node.type
  if (Array.isArray(node.type)) return node.type.find((item) => item !== 'null') ?? null
  if (Array.isArray(node.enum)) return 'string'
  if (node.properties) return 'object'
  if (node.items) return 'array'
  return null
}

function numericBound(node: JsonSchemaNode, kind: 'min' | 'max'): number | null {
  if (kind === 'min') {
    const exclusive = node.exclusiveMinimum
    if (typeof exclusive === 'number') return exclusive
    return asNumber(node.minimum)
  }
  const exclusive = node.exclusiveMaximum
  if (typeof exclusive === 'number') return exclusive
  return asNumber(node.maximum)
}

function readGroup(
  name: string,
  node: JsonSchemaNode,
  hints?: { id: string; title: string; fields: string[] }[],
): string | null {
  const fromNode = asString(node['x-ui-group'] ?? node['x_ui_group'])
  if (fromNode) return fromNode
  const fromHint = hints?.find((group) => group.fields.includes(name))
  return fromHint?.id ?? null
}

function deriveGroups(fields: ParameterField[]): ParameterFormModel['groups'] {
  const windowFields = fields.filter((field) => isDateField(field.name, {})).map((field) => field.name)
  const rest = fields.filter((field) => !windowFields.includes(field.name)).map((field) => field.name)
  const groups: ParameterFormModel['groups'] = []
  if (windowFields.length) groups.push({ id: 'window', title: '样本窗口', fields: windowFields })
  if (rest.length) groups.push({ id: 'parameters', title: '因子参数', fields: rest })
  return groups
}

function humanize(name: string): string {
  return name.replace(/_/g, ' ')
}

function isEmptyValue(value: unknown): boolean {
  if (value == null) return true
  if (typeof value === 'string') return value.trim() === ''
  if (Array.isArray(value)) return value.length === 0
  return false
}

function cloneValue(value: unknown): unknown {
  if (Array.isArray(value)) return [...value]
  const rec = asRecord(value)
  if (rec) return { ...rec }
  return value
}

export function readUiGroups(value: unknown): { id: string; title: string; fields: string[] }[] {
  const rec = asRecord(value)
  const groups = rec ? asArray(rec.groups ?? rec.parameter_groups) : asArray(value)
  return groups.map((item) => {
    const row = asRecord(item)
    if (!row) return null
    const id = asString(row.id) ?? asString(row.title)
    if (!id) return null
    return {
      id,
      title: asString(row.title) ?? id,
      fields: asStringArray(row.fields),
    }
  }).filter((item): item is { id: string; title: string; fields: string[] } => item !== null)
}

export function coerceWidgetValue(field: ParameterField, raw: unknown): unknown {
  if (raw == null) {
    if (field.widget === 'boolean') return false
    if (field.widget === 'multi_enum' || (field.widget === 'symbol_list' && field.multiple)) return []
    if (field.widget === 'symbol_list') return ''
    return ''
  }
  if (field.widget === 'boolean') return asBoolean(raw) ?? false
  if (field.widget === 'integer') return asNumber(raw)
  if (field.widget === 'number') return asNumber(raw)
  if (field.widget === 'multi_enum') return asStringArray(raw)
  if (field.widget === 'symbol_list') {
    if (field.multiple) return asStringArray(raw)
    if (Array.isArray(raw)) return asString(raw[0]) ?? ''
    return asString(raw) ?? ''
  }
  if (field.widget === 'enum' || field.widget === 'date') return asString(raw) ?? ''
  return raw
}
