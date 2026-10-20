/** A股涨跌停幅度（小数）— 与后端 price_limits.py 当前口径一致。
 *
 * 沪深主板 10%（2026-07-06 起 ST 亦为 10%，不再有 5% 档）、
 * 创业板/科创板 20%、北交所 30%。
 *
 * 仅覆盖股票。ETF/指数（跨境、债券 ETF 等）无统一涨跌停规则，
 * 由调用方传 null，分时图按实际数据区间显示。
 */
export function boardLimitRate(symbol: string): number {
  if (symbol.endsWith('.BJ')) return 0.30
  if (/^(300|301|302|688|689)/.test(symbol)) return 0.20
  return 0.10
}
