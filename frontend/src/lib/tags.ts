/** 逗号分隔的标签存储串 → 标签数组 (自选 parquet tags 字段口径)。 */
export function splitTags(s?: string | null): string[] {
  return (s || '').split(',').map(t => t.trim()).filter(Boolean)
}
