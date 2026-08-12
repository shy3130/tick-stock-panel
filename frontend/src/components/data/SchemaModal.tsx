import { AnimatePresence, motion } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { api, type EnrichedField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

const TABLE_TITLES: Record<string, string> = {
  instruments: '个股维表',
  daily: '日 K',
  adj_factor: '除权因子',
  enriched: 'Enriched',
  minute: '分钟 K',
  index_instruments: '指数维表',
  index_daily: '指数日 K',
  index_enriched: '指数 Enriched',
  etf_instruments: 'ETF 维表',
  etf_daily: 'ETF 日 K',
  etf_enriched: 'ETF Enriched',
}

function categorize(name: string): string {
  if (['symbol', 'date'].includes(name)) return '基础'
  if (['open', 'high', 'low', 'close', 'volume', 'amount'].includes(name)) return '行情'
  if (name.startsWith('raw_') || name.startsWith('ex_') || name === 'close_pre_adj') return '复权'
  if (name.startsWith('ma')) return '均线 MA'
  if (name.startsWith('ema')) return '指数均线 EMA'
  if (name.startsWith('macd')) return 'MACD'
  if (name.startsWith('boll')) return '布林带'
  if (name.startsWith('kdj')) return 'KDJ'
  if (name.startsWith('rsi')) return 'RSI'
  if (name.startsWith('signal_') || name.startsWith('consecutive_')) return '信号'
  if (['atr_14', 'vol_ratio_5d', 'vol_ma5', 'vol_ma10', 'momentum_5d', 'momentum_10d', 'momentum_20d', 'momentum_30d', 'momentum_60d', 'annual_vol_20d', 'change_pct', 'change_amount', 'amplitude', 'turnover_rate'].includes(name)) return '波动/动量'
  if (['high_60d', 'low_60d'].includes(name)) return '极值'
  return '其他'
}

export function EnrichedSchemaModal({ table, onClose }: { table: string | null; onClose: () => void }) {
  const open = !!table
  const backdrop = useDialogBackdrop(onClose)
  const schema = useQuery({
    queryKey: QK.tableSchema(table!),
    queryFn: () => api.enrichedSchema(table!),
    enabled: open,
    staleTime: Infinity,
  })

  const fields = schema.data ?? []

  const groups: Record<string, EnrichedField[]> = {}
  for (const f of fields) {
    const cat = categorize(f.name)
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(f)
  }

  const title = table ? (TABLE_TITLES[table] ?? table) : ''

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
        >
          <div className="absolute inset-0 bg-black/40" {...backdrop} />
          <motion.div
            className="relative w-full max-w-xl max-h-[70vh] rounded-card border border-border bg-surface shadow-xl overflow-hidden mx-4"
            initial={{ opacity: 0, scale: 0.95, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="flex items-center justify-between px-5 py-3 border-b border-border">
              <h3 className="text-sm font-medium text-foreground">{title} 字段说明</h3>
              <span className="text-[10px] text-muted font-mono">{fields.length} 个字段</span>
            </div>
            <div className="px-5 py-3 overflow-y-auto max-h-[calc(70vh-48px)]">
              {schema.isLoading ? (
                <div className="text-xs text-muted animate-pulse py-4 text-center">加载中…</div>
              ) : (
                <div className="space-y-3">
                  {Object.entries(groups).map(([cat, items]) => (
                    <div key={cat}>
                      <div className="text-[10px] font-medium text-accent/70 uppercase tracking-wider mb-1.5">{cat}</div>
                      <div className="space-y-1">
                        {items.map((f) => (
                          <div key={f.name} className="flex items-baseline gap-2 text-[11px]">
                            <span className="font-mono text-foreground shrink-0 min-w-[160px]">{f.name}</span>
                            <span className="text-secondary">{f.desc}</span>
                            <span className="text-muted ml-auto shrink-0">{f.type}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
