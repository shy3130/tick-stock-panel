import type { InstrumentSearchResult } from '@/lib/api'

const ASSET_LABEL: Record<string, string> = {
  stock: 'A股',
  index: '指数',
  etf: 'ETF',
  hk: '港股',
  unknown: '未知',
}

const SOURCE_LABEL: Record<string, string> = {
  local: '本地',
  eastmoney_suggest: '东财',
}

const MATCH_LABEL: Record<string, string> = {
  code: '代码',
  symbol: '代码',
  name: '名称',
  pinyin: '拼音',
  initials: '简拼',
  suggest: '补全',
}

export function instrumentSearchMeta(result: InstrumentSearchResult): string {
  const parts = [
    result.asset_type ? (ASSET_LABEL[result.asset_type] ?? result.asset_type) : '',
    result.source ? (SOURCE_LABEL[result.source] ?? result.source) : '',
    result.matched_by ? (MATCH_LABEL[result.matched_by] ?? result.matched_by) : '',
  ].filter(Boolean)
  return parts.join(' · ')
}
