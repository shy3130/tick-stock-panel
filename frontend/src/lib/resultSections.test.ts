import {
  RESULT_SECTIONS,
  forPrint,
  getResultSection,
  normalizeCollapsed,
  sectionAnchorId,
  toggleSection,
  visibleResultSections,
} from './resultSections.ts'

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}

// 注册表自身健康度: key 唯一、标题非空、锚点 id 稳定
const keys = RESULT_SECTIONS.map(section => section.key)
assert(new Set(keys).size === keys.length, '注册表 key 不得重复')
assert(RESULT_SECTIONS.every(section => section.title.length > 0), '注册表标题不得为空')
assert(keys.every(key => sectionAnchorId(key) === `backtest-section-${key}`), '锚点 id 必须由 key 派生')
assert(getResultSection('stats').title === '统计', '按 key 取定义')
try {
  getResultSection('no_such_section')
  throw new Error('不应到达: 未知 key 必须抛错')
} catch (error) {
  assert((error as Error).message.includes('no_such_section'), '未知 key 报错需包含 key 名')
}

const allContext = {
  variant: 'position' as const,
  hasEquityCurve: true,
  hasReturnDistribution: true,
  hasTrades: true,
  hasAttribution: true,
  hasRobustness: true,
}
assert(
  visibleResultSections(allContext).map(section => section.key).join(',') === keys.join(','),
  '仓位模拟全量上下文应按注册顺序返回全部区块',
)

// 候选独立执行: 无专业诊断/稳健性族区块
const candidateKeys = visibleResultSections({
  ...allContext,
  variant: 'candidate',
}).map(section => section.key)
assert(!candidateKeys.includes('professional'), '候选执行不渲染专业诊断')
assert(!candidateKeys.includes('robustness'), '候选执行不渲染稳健性')
assert(!candidateKeys.includes('regime'), '候选执行不渲染市场状态')
assert(!candidateKeys.includes('cost_sensitivity'), '候选执行不渲染成本敏感性')
assert(!candidateKeys.includes('style'), '候选执行不渲染风格归因')
assert(candidateKeys.includes('stats') && candidateKeys.includes('trades'), '候选执行仍保留统计与交易明细')

// 旧全量模拟(历史缓存): 只保留统计/曲线/分布
const legacyKeys = visibleResultSections({
  ...allContext,
  variant: 'legacy-full',
}).map(section => section.key)
assert(
  legacyKeys.join(',') === 'stats,nav_chart,return_distribution',
  '旧全量分支应只包含统计/净值曲线/收益分布',
)

// 数据缺失时对应区块隐藏
const emptyKeys = visibleResultSections({
  variant: 'position',
  hasEquityCurve: false,
  hasReturnDistribution: false,
  hasTrades: false,
  hasAttribution: false,
  hasRobustness: false,
}).map(section => section.key)
assert(emptyKeys.join(',') === 'stats,professional,trust', '无数据上下文只保留恒显区块')

// 折叠状态: toggle 增删
assert(toggleSection([], 'stats').join() === 'stats', '折叠一个区块')
assert(toggleSection(['stats'], 'stats').length === 0, '再次点击恢复展开')
assert(toggleSection(['nav_chart'], 'stats').join(',') === 'nav_chart,stats', '折叠记录追加保序')
assert(toggleSection(['nav_chart', 'stats'], 'nav_chart').join(',') === 'stats', '删除中间记录不影响其余')
assert(forPrint().length === 0, '打印时全部展开')
assert(toggleSection(forPrint(), 'stats').length === 1, '打印态可作为普通折叠输入')

// storage 恢复清洗: 过滤非法与未知 key, 去重
assert(normalizeCollapsed(undefined).length === 0, '非数组输入返回空')
assert(normalizeCollapsed('stats').length === 0, '字符串输入返回空')
assert(normalizeCollapsed(['stats', 3, null, 'ghost', 'trades']).join(',') === 'stats,trades', '过滤非字符串与未注册 key')
assert(normalizeCollapsed(['stats', 'stats', 'trades']).join(',') === 'stats,trades', '重复 key 去重')

console.log('resultSections.test.ts ok')
