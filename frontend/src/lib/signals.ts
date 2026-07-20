/**
 * 买卖触发器信号定义 — 选股页弹窗 / 回测页共用。
 *
 * 信号 ID 必须与后端 backtest/strategy.py:_build_signal_mask 对齐
 * (signal_* 前缀为内置原子信号, csg_ 前缀为用户自定义信号)。
 */

export type SignalKind = 'entry' | 'exit' | 'both'

export interface BuiltinSignalDefinition {
  id: string
  name: string
  kind: SignalKind
  category: string
  description: string
}

/** 内置原子信号清单 (权威展示来源, 两页统一) */
export const BUILTIN_SIGNAL_DEFINITIONS: BuiltinSignalDefinition[] = [
  {
    id: 'signal_ma_golden_5_20',
    name: 'MA5上穿MA20',
    kind: 'entry',
    category: '均线',
    description: '短期均线 MA5 上穿中期均线 MA20，常用于趋势转强确认。',
  },
  {
    id: 'signal_ma_dead_5_20',
    name: 'MA5下穿MA20',
    kind: 'exit',
    category: '均线',
    description: '短期均线 MA5 下穿中期均线 MA20，常用于趋势转弱或止盈止损。',
  },
  {
    id: 'signal_ma_golden_20_60',
    name: 'MA20上穿MA60',
    kind: 'entry',
    category: '均线',
    description: '中期均线 MA20 上穿长期均线 MA60，偏中线趋势信号。',
  },
  {
    id: 'signal_macd_golden',
    name: 'MACD金叉',
    kind: 'entry',
    category: 'MACD',
    description: 'MACD DIF 上穿 DEA，表示动能可能由弱转强。',
  },
  {
    id: 'signal_macd_dead',
    name: 'MACD死叉',
    kind: 'exit',
    category: 'MACD',
    description: 'MACD DIF 下穿 DEA，表示动能可能由强转弱。',
  },
  {
    id: 'signal_ma20_breakout',
    name: '突破MA20',
    kind: 'entry',
    category: '趋势',
    description: '收盘价向上突破 MA20，常用于趋势突破买点。',
  },
  {
    id: 'signal_ma20_breakdown',
    name: '跌破MA20',
    kind: 'exit',
    category: '趋势',
    description: '收盘价向下跌破 MA20，常用于趋势破位卖点。',
  },
  {
    id: 'signal_ma5_breakout',
    name: '突破MA5',
    kind: 'entry',
    category: '趋势',
    description: '收盘价向上突破 MA5，偏短线转强或回踩企稳买点。',
  },
  {
    id: 'signal_ma5_breakdown',
    name: '跌破MA5',
    kind: 'exit',
    category: '趋势',
    description: '收盘价向下跌破 MA5，偏短线转弱或止盈止损卖点。',
  },
  {
    id: 'signal_ma10_breakout',
    name: '突破MA10',
    kind: 'entry',
    category: '趋势',
    description: '收盘价向上突破 MA10，偏短中线转强或突破买点。',
  },
  {
    id: 'signal_ma10_breakdown',
    name: '跌破MA10',
    kind: 'exit',
    category: '趋势',
    description: '收盘价向下跌破 MA10，偏短中线转弱或破位卖点。',
  },
  {
    id: 'signal_n_day_high',
    name: '60日新高',
    kind: 'entry',
    category: '趋势',
    description: '收盘价创近 60 日新高，表示阶段强势或突破。',
  },
  {
    id: 'signal_n_day_low',
    name: '60日新低',
    kind: 'exit',
    category: '趋势',
    description: '收盘价创近 60 日新低，表示阶段弱势或风险释放。',
  },
  {
    id: 'signal_boll_breakout_upper',
    name: '突破布林上轨',
    kind: 'entry',
    category: 'BOLL',
    description: '价格突破布林上轨，偏强势突破或加速信号。',
  },
  {
    id: 'signal_boll_breakdown_lower',
    name: '跌破布林下轨',
    kind: 'exit',
    category: 'BOLL',
    description: '价格跌破布林下轨，偏弱势破位或超跌风险信号。',
  },
  {
    id: 'signal_volume_surge',
    name: '放量',
    kind: 'both',
    category: '量价',
    description: '成交量显著放大，可作为入场确认、出场确认或告警条件。',
  },
  {
    id: 'signal_limit_up',
    name: '涨停',
    kind: 'entry',
    category: '涨跌停',
    description: '收盘封住涨停，用于强势股、连板与市场情绪监控。',
  },
  {
    id: 'signal_limit_down',
    name: '跌停',
    kind: 'exit',
    category: '涨跌停',
    description: '收盘触及跌停，用于风险控制与弱势监控。',
  },
  {
    id: 'signal_limit_down_recovery',
    name: '跌停翘板',
    kind: 'entry',
    category: '涨跌停',
    description: '盘中触及跌停后回升，常用于短线情绪修复观察。',
  },
  {
    id: 'signal_broken_limit_up',
    name: '炸板',
    kind: 'exit',
    category: '涨跌停',
    description: '盘中触及涨停但收盘未封住，用于强转弱或分歧监控。',
  },
]

export const MONITOR_INTRADAY_SIGNAL_LABELS: Record<string, string> = {
  signal_intraday_avg_cross_up: '分时价格上穿均价',
  signal_intraday_avg_cross_down: '分时价格下穿均价',
  signal_intraday_zero_cross_up: '分时价格上穿0轴',
  signal_intraday_zero_cross_down: '分时价格下穿0轴',
}

export const MONITOR_INTRADAY_SIGNAL_OPTIONS = Object.keys(MONITOR_INTRADAY_SIGNAL_LABELS)

/** 内置原子信号 → 中文标签 */
export const SIGNAL_LABELS: Record<string, string> = BUILTIN_SIGNAL_DEFINITIONS.reduce<Record<string, string>>((acc, sig) => {
  acc[sig.id] = sig.name
  return acc
}, { ...MONITOR_INTRADAY_SIGNAL_LABELS })

/** 内置信号 ID 列表 */
export const SIGNAL_OPTIONS = BUILTIN_SIGNAL_DEFINITIONS.map(sig => sig.id)

/** 常用技术指标/字段 → 中文 (阈值条件展示用, 与后端 ENRICHED_COLUMNS 对齐) */
const FIELD_LABELS: Record<string, string> = {
  close: '收盘价', open: '开盘价', high: '最高价', low: '最低价',
  change_pct: '涨跌幅', change_amount: '涨跌额', amplitude: '振幅',
  turnover_rate: '换手率', volume: '成交量', amount: '成交额',
  ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma30: 'MA30', ma60: 'MA60',
  ema5: 'EMA5', ema10: 'EMA10', ema20: 'EMA20',
  macd_dif: 'MACD-DIF', macd_dea: 'MACD-DEA', macd_hist: 'MACD柱',
  boll_upper: '布林上轨', boll_lower: '布林下轨',
  kdj_k: 'KDJ-K', kdj_d: 'KDJ-D', kdj_j: 'KDJ-J',
  rsi_6: 'RSI6', rsi_14: 'RSI14', rsi_24: 'RSI24',
  vol_ratio_5d: '5日量比', vol_ratio_20d: '20日量比',
  vol_ma5: '5日均量', vol_ma10: '10日均量',
  high_60d: '60日最高', low_60d: '60日最低',
  momentum_5d: '5日动量', momentum_20d: '20日动量', momentum_60d: '60日动量',
  atr_14: 'ATR14', annual_vol_20d: '20日年化波动',
  consecutive_limit_ups: '连板数', consecutive_limit_downs: '跌停连板',
}

/**
 * 信号/字段 ID → 中文显示名。
 * 内置信号查 SIGNAL_LABELS; csg_ 前缀查传入的自定义信号名称映射;
 * 技术指标查 FIELD_LABELS; 都找不到则原样返回。
 */
export function cnSignal(name: string, customNames?: Record<string, string>): string {
  if (customNames && name in customNames) return customNames[name]
  return SIGNAL_LABELS[name] ?? FIELD_LABELS[name] ?? name
}
