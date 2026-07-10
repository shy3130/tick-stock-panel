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
