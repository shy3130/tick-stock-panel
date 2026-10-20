import type { CapabilityRoute, ProviderField } from '@/lib/api'

/** 全部可路由偏好字段的默认生效源。 */
export const DEFAULT_ROUTING: Record<ProviderField, string> = {
  daily_data_provider: 'tickflow',
  adj_factor_provider: 'tickflow',
  minute_data_provider: 'tickflow',
  full_minute_data_provider: 'tickflow',
  depth5_data_provider: 'tickflow',
  realtime_data_provider: 'tickflow',
  financial_data_provider: 'tickflow',
}

/**
 * 计算「一键套用」的目标路由: 该源声明了哪个数据集, 就接管对应能力;
 * 未声明的能力回落默认源, 避免残留上一个数据源的配置。
 *
 * 能力清单取自后端能力矩阵 (dataset id 与能力 field 的单一权威), 不在前端
 * 手写字段列表 —— 手写清单会漏掉新增能力 (depth5 / full_minute 就这样被漏过,
 * 点「套用」后这两个能力仍停在旧数据源)。
 *
 * 矩阵未加载时 fail-closed 抛错, 而不是把全部路由重置成默认源。
 */
export function buildProviderRouting(
  provider: string,
  datasets: readonly string[],
  capabilities: readonly Pick<CapabilityRoute, 'id' | 'field'>[],
): Record<ProviderField, string> {
  if (!capabilities.length) {
    throw new Error('能力矩阵未加载, 无法套用数据源, 请稍后重试')
  }
  const supported = new Set(datasets)
  const routing: Record<ProviderField, string> = { ...DEFAULT_ROUTING }
  for (const cap of capabilities) {
    if (!cap.field) continue // 不可路由能力 (仅 TickFlow 提供) 没有偏好字段
    routing[cap.field] = supported.has(cap.id) ? provider : DEFAULT_ROUTING[cap.field]
  }
  return routing
}
