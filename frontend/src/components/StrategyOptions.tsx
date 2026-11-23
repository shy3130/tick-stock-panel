import { selectableStrategies, strategyChoiceGroups, strategyOptionLabel, type StrategyChoice } from '@/lib/strategyLibrary'

/** 原生选择框共用分组与归档规则, 不改变各页面已有的策略兼容性筛选。 */
export function StrategyOptions({ strategies, selected = [] }: {
  strategies: readonly StrategyChoice[]
  selected?: readonly string[]
}) {
  const groups = strategyChoiceGroups(selectableStrategies(strategies, selected))
  return <>{groups.map(group => group.id
    ? <optgroup key={group.id} label={group.name}>{group.items.map(item => (
        <option key={item.id} value={item.id}>{strategyOptionLabel(item)}</option>
      ))}</optgroup>
    : group.items.map(item => <option key={item.id} value={item.id}>{strategyOptionLabel(item)}</option>))}</>
}
