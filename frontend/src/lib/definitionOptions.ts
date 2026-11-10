/** 归档只改变新选择的可见性, 不移除已保存配置中的定义引用。 */
export function selectableDefinitions<T extends { id: string; archived?: boolean }>(items: readonly T[], selected: readonly string[] = []): T[] {
  const keep = new Set(selected)
  return items.filter(item => !item.archived || keep.has(item.id))
}

export function definitionOptionLabel(item: { label: string; archived?: boolean }): string {
  return `${item.label}${item.archived ? '（已归档）' : ''}`
}
