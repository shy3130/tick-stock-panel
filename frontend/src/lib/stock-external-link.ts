/**
 * 个股详情外链 — 可配置 URL 模板 + 证券代码。
 *
 * 占位符保持独立, 用户自行排列 (市场前缀/后缀因站而异):
 *   {code}   纯 6 位代码, 如 000001
 *   {market} 市场前缀小写, 如 sz / sh / bj
 *   {symbol} 完整 symbol (含交易所后缀), 如 000001.SZ
 * 模板留空 = 关闭外链。
 */
import { storage } from '@/lib/storage'
import { setUiPref } from '@/lib/uiPrefs'

/** 形状守卫: 只放行 6位数字 + 沪深北后缀 的 symbol (信息条场景下即股票) */
const STOCK_SYMBOL_RE = /^(\d{6})\.(SH|SZ|BJ)$/

/** 未设置或留空 → 返回 '' 即关闭外链 */
export function loadStockExternalTemplate(): string {
  return storage.stockExternalTemplate.get('')
}

export function saveStockExternalTemplate(tpl: string): void {
  // 经 setUiPref 双写: localStorage 供同步读取, 后端副本保证换端口/换机器后仍能恢复
  setUiPref('stock_external_template', JSON.stringify(tpl.trim()))
}

export function buildStockExternalUrl(template: string, symbol: string): string | null {
  if (!template) return null
  // scheme 白名单: 只放行 http/https, 挡掉 javascript:/data: 等危险 scheme
  if (!/^https?:\/\//i.test(template.trim())) return null
  const m = symbol.match(STOCK_SYMBOL_RE)
  if (!m) return null
  const code = m[1]
  const market = m[2].toLowerCase()
  return template
    .replaceAll('{code}', code)
    .replaceAll('{market}', market)
    .replaceAll('{symbol}', symbol)
}
