import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Globe, Loader2 } from 'lucide-react'
import { api, chartLiveDegraded, type MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EChartsIntraday } from '@/components/EChartsIntraday'

interface Props {
  symbol: string
  date: string | null
  height?: number
  prevClose?: number
  className?: string
  onPriceHover?: (price: number | null) => void
}

export function StockIntradayChart({
  symbol,
  date,
  height = 520,
  prevClose,
  className,
  onPriceHover,
}: Props) {
  const qc = useQueryClient()
  const [minuteDismissed, setMinuteDismissed] = useState(false)

  const minute = useQuery({
    queryKey: QK.klineMinute(symbol, date ?? ''),
    queryFn: () => api.klineMinute(symbol, date ?? undefined),
    enabled: !!symbol && !!date,
  })

  const fetchMinute = useMutation({
    mutationFn: () => api.extendMinuteHistory(5, 'day'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kline-minute', symbol] })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      setMinuteDismissed(false)
    },
  })

  const minuteRows: MinuteKlineRow[] = useMemo(() => minute.data?.rows ?? [], [minute.data?.rows])
  // source=none 表示本地无数据且上游也拉不到 (停牌/复牌延迟/非交易日)
  // 此时不弹"是否获取"询问窗, 只做静态提示, 避免误导用户去拉明知拉不到的数据
  const sourceIsNone = minute.data?.source === 'none'
  // 指数分钟K无本地存储且不支持落库获取 (后端 sync_minute_single 显式拒绝), 不显示获取按钮
  const isIndex = minute.data?.asset_type === 'index'
  const liveDegraded = chartLiveDegraded(minute.data)
  const chartHeight = liveDegraded ? Math.max(height - 22, 80) : height


  useEffect(() => {
    setMinuteDismissed(false)
    onPriceHover?.(null)
  }, [date, onPriceHover])

  if (!symbol || !date) return null

  return (
    <div className={className} style={{ height, flexShrink: 0 }}>
      {liveDegraded && (
        <div className="px-1 pb-1">
          <span
            className="inline-flex cursor-help items-center gap-1 rounded border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[9px] leading-none text-warning/80"
            title="分时来自腾讯公共行情，为盘中临时数据，仅供展示；不会写入本地行情库，也不参与选股、监控、回测。"
          >
            <Globe className="h-2.5 w-2.5" aria-hidden />
            外部源·盘中临时数据
          </span>
        </div>
      )}
      {minute.isLoading && <div className="text-xs text-muted py-2">分时加载中…</div>}
      {!minute.isLoading && minuteRows.length === 0 && (
        <>
          {fetchMinute.isPending ? (
            <div className="flex items-center justify-center h-full gap-2 text-xs text-accent">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>正在获取最近5日分钟K…</span>
            </div>
          ) : isIndex ? (
            // 指数: 分钟K仅支持实时读取, 无落库获取入口
            <div className="flex items-center justify-center h-full text-xs text-muted">指数暂无分钟数据</div>
          ) : sourceIsNone ? (
            // 数据源确认无此日分钟数据 (停牌/复牌延迟等): 静态提示 + 保留重试
            <div className="flex flex-col items-center justify-center h-full gap-3">
              <div className="text-xs text-muted">该日暂无分钟数据（数据源未提供）</div>
              <button
                onClick={() => fetchMinute.mutate()}
                className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs font-medium hover:bg-elevated/80 transition-colors duration-150"
              >
                重新获取
              </button>
            </div>
          ) : minuteDismissed ? (
            <div className="flex flex-col items-center justify-center h-full gap-3">
              <div className="text-xs text-muted">暂无分钟数据</div>
              <button
                onClick={() => setMinuteDismissed(false)}
                className="px-4 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent transition-colors duration-150"
              >
                获取分钟K
              </button>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-full gap-4">
              <div className="text-sm text-foreground">是否立即获取最近5日分钟K？</div>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => fetchMinute.mutate()}
                  className="px-4 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent transition-colors duration-150"
                >
                  确定
                </button>
                <button
                  onClick={() => setMinuteDismissed(true)}
                  className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors duration-150"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </>
      )}
      {minuteRows.length > 0 && (
        <EChartsIntraday
          data={minuteRows}
          height={chartHeight}
          prevClose={prevClose}
          date={date}
          symbol={symbol}
          onPriceHover={onPriceHover}
        />
      )}
    </div>
  )
}
