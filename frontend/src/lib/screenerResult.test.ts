// screenerResult 纯逻辑单测 — bun 直跑: bun src/lib/screenerResult.test.ts
import {
  buildScreenerQueryRequest,
  csvEscapeCell,
  effectiveGroupLogic,
  facetWarningText,
  industryFacetDisplay,
  isLatestOnlyField,
  latestOnlyBadgeLabel,
  normalizeConditionGroup,
  serializeScreenerConditions,
  splitConditionsForAsOf,
  toResultCsv,
  uniqueConditionGroups,
} from './screenerResult.ts'
import type { ScreenerCondition, ScreenerFieldSpec } from './api'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

function spec(
  field: string,
  availability: ScreenerFieldSpec['availability'],
  extra: Partial<ScreenerFieldSpec> = {},
): ScreenerFieldSpec {
  return {
    field,
    label: field,
    group: 'price',
    source: 'enriched',
    value_type: 'numeric',
    null_policy: '',
    availability,
    ops: ['>', '<'],
    sortable: true,
    ...extra,
  }
}

// ── csvEscapeCell ──

assert(csvEscapeCell('贵州茅台') === '贵州茅台', '普通文本不转义')
assert(csvEscapeCell('12.5') === '12.5', '数字串不转义')
assert(csvEscapeCell('a,b') === '"a,b"', '含逗号用双引号包裹')
assert(csvEscapeCell('say "hi"') === '"say ""hi"""', '引号双写转义')
assert(csvEscapeCell('line1\nline2') === '"line1\nline2"', '换行触发包裹')
assert(csvEscapeCell('crlf\r\nx') === '"crlf\r\nx"', 'CRLF 触发包裹')

// ── toResultCsv ──

const columns = [
  { field: 'symbol', label: '代码' },
  { field: 'name', label: '名称' },
  { field: 'close', label: '收盘' },
]

assert(
  toResultCsv(columns, [{ symbol: '600519.SH', name: '贵州茅台', close: 1700.5 }])
    === '代码,名称,收盘\r\n600519.SH,贵州茅台,1700.5',
  '表头与行按列顺序输出，CRLF 分隔',
)
assert(
  toResultCsv(columns, [{ symbol: 'A' }, { symbol: 'B', name: '带,逗号', close: 1 }])
    === '代码,名称,收盘\r\nA,,\r\nB,"带,逗号",1',
  '空值导出为空串，值内逗号转义',
)
assert(toResultCsv(columns, []) === '代码,名称,收盘', '空结果只有表头')

// ── latestOnlyBadgeLabel ──

assert(latestOnlyBadgeLabel({ availability: 'latest_only' }) === '仅最新日', 'latest_only 返回徽标文案')
assert(latestOnlyBadgeLabel({ availability: 'available' }) === null, 'available 无徽标')
assert(latestOnlyBadgeLabel({ availability: 'unavailable' }) === null, 'unavailable 无徽标')

// ── splitConditionsForAsOf ──

const fieldsByName = new Map<string, ScreenerFieldSpec>([
  ['turnover_rate', spec('turnover_rate', 'available')],
  ['float_market_cap', spec('float_market_cap', 'latest_only')],
  ['exclude_st', spec('exclude_st', 'latest_only')],
  // sequence 字段：历史可用，不进 latest_only 剔除集
  ['seq_consecutive_up_3', spec('seq_consecutive_up_3', 'available', { source: 'sequence', group: '多日形态', value_type: 'boolean', ops: ['=', '!='] })],
  ['seq_cum_change_5d', spec('seq_cum_change_5d', 'available', { source: 'sequence', group: '多日形态' })],
])
const conditions: ScreenerCondition[] = [
  { field: 'turnover_rate', op: '>', value: 0.03 },
  { field: 'float_market_cap', op: '>', value: 100 },
  { field: 'exclude_st', op: '=', value: false },
]

{
  const { applicable, droppedCount } = splitConditionsForAsOf(conditions, fieldsByName, false)
  assert(applicable.length === 3 && droppedCount === 0, '最新日查询保留全部条件')
}
{
  const { applicable, droppedCount } = splitConditionsForAsOf(conditions, fieldsByName, true)
  assert(applicable.length === 1 && applicable[0].field === 'turnover_rate' && droppedCount === 2,
    '历史日期剔除仅最新日条件')
}
{
  const { applicable } = splitConditionsForAsOf(
    [{ field: 'unknown_field', op: '>', value: 1 }],
    fieldsByName,
    true,
  )
  assert(applicable.length === 1, '未知字段不算 latest_only，原样保留')
}
{
  const { applicable, droppedCount } = splitConditionsForAsOf(
    [{ field: 'exclude_st', op: '=', value: false }],
    fieldsByName,
    true,
  )
  assert(applicable.length === 0 && droppedCount === 1, '全部为 latest_only 时历史日期下可查集为空')
}
{
  const mixed: ScreenerCondition[] = [
    { field: 'seq_consecutive_up_3', op: '=', value: true },
    { field: 'seq_cum_change_5d', op: '>', value: 5 },
    { field: 'float_market_cap', op: '>', value: 100 },
  ]
  const { applicable, droppedCount } = splitConditionsForAsOf(mixed, fieldsByName, true)
  assert(
    applicable.length === 2
      && applicable.every(c => c.field.startsWith('seq_'))
      && droppedCount === 1,
    'sequence 字段历史日保留，仅剔除 latest_only',
  )
}

// ── isLatestOnlyField ──

assert(isLatestOnlyField('float_market_cap', fieldsByName) === true, '命中 latest_only')
assert(isLatestOnlyField('turnover_rate', fieldsByName) === false, '普通字段 false')
assert(isLatestOnlyField('seq_consecutive_up_3', fieldsByName) === false, 'sequence 非 latest_only')
assert(isLatestOnlyField('nope', fieldsByName) === false, '未知字段 false')
assert(isLatestOnlyField(undefined, fieldsByName) === false, '空值 false')

// ── F14 group normalize / serialize ──

assert(normalizeConditionGroup(undefined) === 'A', '缺省 group → A')
assert(normalizeConditionGroup(null) === 'A', 'null group → A')
assert(normalizeConditionGroup('') === 'A', '空 group → A')
assert(normalizeConditionGroup('b') === 'B', '小写 b → B')
assert(normalizeConditionGroup('C') === 'C', '合法 C 保留')
assert(normalizeConditionGroup('Z') === 'A', '非法组回落 A')

{
  const serialized = serializeScreenerConditions([
    { field: 'turnover_rate', op: '>', value: 0.03 },
    { field: 'change_pct', op: '>', value: 0.05, group: 'b' },
  ])
  assert(serialized[0].group === 'A' && serialized[1].group === 'B', '序列化规范化 group')
  assert(serialized[0].field === 'turnover_rate' && serialized[0].value === 0.03, '序列化保留 field/value')
}

assert(
  uniqueConditionGroups([
    { field: 'a', op: '=', value: 1, group: 'A' },
    { field: 'b', op: '=', value: 2, group: 'B' },
    { field: 'c', op: '=', value: 3 },
  ]).join('') === 'AB',
  'unique groups 去重并按 A-E 序；缺省算 A',
)

// 旧条件默认 AND：单组即使 UI 选 or 也强制 and
assert(
  effectiveGroupLogic([{ field: 'a', op: '=', value: 1 }, { field: 'b', op: '=', value: 2, group: 'A' }], 'or') === 'and',
  '单组 or 仍 effective and（兼容旧 flat AND）',
)
assert(
  effectiveGroupLogic(
    [{ field: 'a', op: '=', value: 1, group: 'A' }, { field: 'b', op: '=', value: 2, group: 'B' }],
    'or',
  ) === 'or',
  '双组保留 or',
)
assert(
  effectiveGroupLogic(
    [{ field: 'a', op: '=', value: 1, group: 'A' }, { field: 'b', op: '=', value: 2, group: 'B' }],
    undefined,
  ) === 'and',
  '双组缺省 group_logic → and',
)

// A/B 两组 OR request serialization
{
  const request = buildScreenerQueryRequest({
    conditions: [
      { field: 'turnover_rate', op: '>', value: 0.03, group: 'A' },
      { field: 'change_pct', op: '>', value: 0.05, group: 'B' },
    ],
    limit: 50,
    asOf: '2026-08-19',
    orderBy: { field: 'change_pct', direction: 'desc' },
    groupLogic: 'or',
  })
  assert(request.group_logic === 'or', 'A/B OR 请求 group_logic=or')
  assert(request.facets?.length === 1 && request.facets[0] === 'industry', '请求固定 facets industry')
  assert(request.conditions[0].group === 'A' && request.conditions[1].group === 'B', '请求带规范化 group')
  assert(request.as_of === '2026-08-19' && request.limit === 50, 'as_of/limit 透传')
  assert(request.order_by?.field === 'change_pct' && request.order_by.direction === 'desc', 'order_by 透传')
}

// 旧条件默认 AND request
{
  const request = buildScreenerQueryRequest({
    conditions: [
      { field: 'turnover_rate', op: '>', value: 0.03 },
      { field: 'change_pct', op: '>', value: 0.05 },
    ],
    limit: 100,
  })
  assert(request.group_logic === 'and', '旧条件无 group 时 group_logic=and')
  assert(request.conditions.every(c => c.group === 'A'), '旧条件序列化为 group=A')
  assert(JSON.stringify(request.facets) === JSON.stringify(['industry']), '默认 facets industry')
  assert(request.as_of === undefined && request.order_by === undefined, '可选字段缺省不出现')
}

// ── F15 facet count → 展示数据 ──

assert(industryFacetDisplay(undefined) === null, '无 facets → 隐藏')
assert(industryFacetDisplay({}) === null, '空 facets 对象 → 隐藏')
assert(industryFacetDisplay({ industry: [] }) === null, '空 industry 数组 → 隐藏')

{
  const display = industryFacetDisplay({
    industry: [
      { value: '银行', count: 3 },
      { value: '电子', count: 10 },
      { value: '  ', count: 2 },
      { value: '医药', count: 0 },
    ],
  }, 20)
  assert(display !== null, '有效 facet 应展示')
  assert(display!.items.length === 2, '空名/0 count 过滤')
  assert(display!.items[0][0] === '电子' && display!.items[0][1] === 10, '按 count desc')
  assert(display!.items[1][0] === '银行' && display!.items[1][1] === 3, '次高银行')
  assert(display!.total === 20 && display!.missing === 7 && display!.max === 10, 'total/missing/max 由 facet+结果 total 推导')
}

{
  const display = industryFacetDisplay({ industry: [{ value: '银行', count: 5 }] })
  assert(display !== null && display.total === 5 && display.missing === 0, '无 resultTotal 时 total=covered')
}

assert(facetWarningText('industry_unavailable') === '行业分布暂不可用（财务快照缺失）', '已知 warning 文案')
assert(facetWarningText('other_code') === 'other_code', '未知 warning 原样回退')

// api.screenerScreens* 客户端（GET/POST/PUT/DELETE /api/screener/screens）是
// request() 的薄封装：URL/方法/JSON 体由后端契约测试覆盖，前端无独立逻辑可断言，跳过。

console.log('screenerResult.test.ts: 全部断言通过')
