/** 分时列「集合竞价叠加」数据加载（自选页/策略页共用）。
 *
 * 竞价价格/匹配量/未匹配量三项必须来自同一张时序扩展表（同一时间网格），
 * 因此按表 + 三个字段一次性取三条序列，再按 symbol 合并成竞价点列。
 * 未配置或字段缺失时不发请求。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { isAuctionActive, type IntradayAuctionConfig } from '@/lib/list-columns'
import type { ColumnConfig } from '@/lib/list-columns'
import type { MiniIntradayAuctionSeries } from '@/components/stock-table/MiniIntraday'

export type AuctionSeriesMap = Record<string, MiniIntradayAuctionSeries>

/** 从列配置里取出生效的竞价叠加配置（分时列可见且三项齐全）。 */
export function useAuctionConfig(columns: ColumnConfig[]): IntradayAuctionConfig | null {
  return useMemo(() => {
    const col = columns.find(c => c.source.type === 'builtin' && c.source.key === 'intraday' && c.visible)
    const cfg = col?.intradayConfig?.auction
    return isAuctionActive(cfg) ? cfg! : null
  }, [columns])
}

/** 拉取竞价点列。symbols 为当前列表标的；date 为空时取扩展表最新分区。 */
export function useAuctionSeries(
  cfg: IntradayAuctionConfig | null,
  symbols: string[],
  date?: string | null,
): AuctionSeriesMap {
  const symbolsKey = useMemo(() => [...new Set(symbols)].sort().join(','), [symbols])
  const cfgKey = cfg
    ? `${cfg.configId}:${cfg.priceField}:${cfg.matchField ?? ''}:${cfg.unmatchedField ?? ''}`
    : ''

  const query = useQuery({
    queryKey: QK.extSeries(`auction:${cfgKey}`, date, symbolsKey),
    queryFn: async () => {
      const c = cfg!
      const syms = symbolsKey ? symbolsKey.split(',') : []
      const [price, match, unmatched] = await Promise.all([
        api.extDataSeries(c.configId!, c.priceField!, date ?? undefined, syms),
        c.matchField
          ? api.extDataSeries(c.configId!, c.matchField, date ?? undefined, syms)
          : Promise.resolve(null),
        c.unmatchedField
          ? api.extDataSeries(c.configId!, c.unmatchedField, date ?? undefined, syms)
          : Promise.resolve(null),
      ])
      const out: AuctionSeriesMap = {}
      for (const [sym, points] of Object.entries(price.items ?? {})) {
        const entry: MiniIntradayAuctionSeries = { price: points }
        const m = match?.items?.[sym]
        if (m?.length) entry.match = m
        const u = unmatched?.items?.[sym]
        if (u?.length) entry.unmatched = u
        out[sym] = entry
      }
      return out
    },
    enabled: !!cfg && symbolsKey.length > 0,
    staleTime: 5 * 60_000,
    placeholderData: prev => prev,
  })

  return cfg ? (query.data ?? {}) : {}
}
