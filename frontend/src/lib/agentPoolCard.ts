/**
 * 从 screen_stock_pool 工具结果中提取股票池卡片数据（fail-closed）。
 * 后端成功时返回 { status:'success', pool_id, count, total, as_of, preview:[{symbol,name}] };
 * 任何字段缺失/非法一律返回 null，不渲染卡片。
 */

export interface AgentPoolCard {
  pool_id: string
  as_of: string
  total: number
  previewSymbols: string[]
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

export function extractPoolCard(result: unknown): AgentPoolCard | null {
  if (!result || typeof result !== 'object') return null
  const r = result as Record<string, unknown>
  if (r.status !== 'success') return null

  const poolId = typeof r.pool_id === 'string' ? r.pool_id.trim() : ''
  const asOf = typeof r.as_of === 'string' && DATE_RE.test(r.as_of) ? r.as_of : ''
  const total = typeof r.total === 'number' && Number.isFinite(r.total) && r.total > 0 ? Math.floor(r.total) : 0
  if (!poolId || !asOf || !total) return null

  const preview = Array.isArray(r.preview) ? r.preview : []
  const previewSymbols: string[] = []
  for (const item of preview) {
    if (!item || typeof item !== 'object') continue
    const symbol = (item as Record<string, unknown>).symbol
    if (typeof symbol === 'string' && symbol.trim()) previewSymbols.push(symbol.trim())
  }

  return { pool_id: poolId, as_of: asOf, total, previewSymbols }
}
