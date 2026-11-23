import type { StrategyDetail } from './api'

/** 信号 id 归一 (与策略回测页一致): 裸名补 signal_ 前缀。 */
export const toSignalId = (sig: string) =>
  sig.startsWith('signal_') || sig.startsWith('csg_') ? sig : `signal_${sig}`

/** 从策略详情构建默认 overrides (basic_filter / 信号 / 风控)。
 * 优化器与策略回测页共用, 保证优化的就是用户当前配置的策略。 */
export function buildDefaultOverrides(detail: StrategyDetail): Record<string, any> {
  return {
    basic_filter: { ...detail.basic_filter },
    entry_signals: detail.entry_signals.map(toSignalId),
    exit_signals: detail.exit_signals.map(toSignalId),
    scoring: { ...detail.scoring },
    scoring_directions: { ...(detail.scoring_directions ?? {}) },
    scoring_replace: true,
    // 叠加条件随策略配置带入: 回测 runner 只读请求 overrides, 不回退落盘配置,
    // 缺了这一项策略页配置的叠加条件会在回测侧静默失效。
    overlay_filter: detail.overlay_filter ?? [],
    stop_loss: detail.stop_loss,
    take_profit: detail.take_profit,
    trailing_stop: detail.trailing_stop,
    trailing_take_profit_activate: detail.trailing_take_profit_activate,
    trailing_take_profit_drawdown: detail.trailing_take_profit_drawdown,
    score_min: null,
    score_max: null,
    max_hold_days: detail.max_hold_days,
  }
}

/** 「应用参数到策略」共享通道: 优化/检验产出的参数 → POST /strategies/config。
 *  与策略回测页"应用到策略"同一持久化路径 (params 合并进 params_defaults,
 *  策略页可查看/还原, 可逆)。 */
export async function applyStrategyParams(
  detail: StrategyDetail,
  params: Record<string, any>,
): Promise<void> {
  const { api } = await import('./api')
  const merged: Record<string, any> = { ...(detail.params_defaults ?? {}) }
  detail.params.forEach(p => {
    if (!(p.id in merged)) merged[p.id] = p.default
  })
  Object.assign(merged, params)
  await api.strategySaveConfig(detail.id, { ...buildDefaultOverrides(detail), params: merged })
}
