import { useEffect, useRef } from 'react'
import {
  createChart,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
} from 'lightweight-charts'
import { useChartTheme } from '@/lib/theme'

export interface OHLC {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
}

export function fmtBigNum(v: number): string {
  if (v >= 1_000_000_000_000) return `${(v / 1_000_000_000_000).toFixed(2)}万亿`
  if (v >= 100_000_000) return `${(v / 100_000_000).toFixed(2)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return v.toFixed(0)
}

// 序列颜色 (双主题通用); 画布文字/网格/边框主题相关色走 ChartTheme
const THEME = {
  background: 'transparent',
  bull: '#F04438',
  bear: '#12B76A',
  volBull: 'rgba(240,68,56,0.4)',
  volBear: 'rgba(18,183,106,0.4)',
}

interface Props {
  data: OHLC[]
  height?: number
}

export function CandlestickChart({ data, height = 480 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const ct = useChartTheme()
  const ctRef = useRef(ct)
  ctRef.current = ct

  // 主题切换: 无需重建 chart, applyOptions 即时生效
  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: { textColor: ct.text },
      grid: { vertLines: { color: ct.grid }, horzLines: { color: ct.grid } },
      rightPriceScale: { borderColor: ct.border },
      timeScale: { borderColor: ct.border },
    })
  }, [ct])

  useEffect(() => {
    if (!containerRef.current) return
    const el = containerRef.current

    const chart = createChart(el, {
      width: el.clientWidth,
      height,
      layout: {
        background: { color: THEME.background },
        textColor: ctRef.current.text,
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: ctRef.current.grid },
        horzLines: { color: ctRef.current.grid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { labelVisible: false },
        horzLine: { labelVisible: false },
      },
      rightPriceScale: { borderColor: ctRef.current.border },
      timeScale: {
        borderColor: ctRef.current.border,
        timeVisible: false,
        secondsVisible: false,
      },
    })

    // Candlestick — occupies top 80% via default price scale
    const candle = chart.addCandlestickSeries({
      upColor: THEME.bull,
      downColor: THEME.bear,
      borderUpColor: THEME.bull,
      borderDownColor: THEME.bear,
      wickUpColor: THEME.bull,
      wickDownColor: THEME.bear,
      lastValueVisible: false,
      priceLineVisible: false,
    })

    // Volume — separate price scale, squeezed to bottom 20%
    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      lastValueVisible: false,
      priceLineVisible: false,
    })
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    })

    chartRef.current = chart
    candleRef.current = candle
    volRef.current = volume

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: el.clientWidth })
    })
    ro.observe(el)

    return () => {
      ro.disconnect()
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      volRef.current = null
    }
  }, [height])

  useEffect(() => {
    if (!chartRef.current || !candleRef.current || !volRef.current || data.length === 0) return

    candleRef.current.setData(
      data.map(d => ({
        time: d.date as any,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      })) as CandlestickData[],
    )

    volRef.current.setData(
      data.map(d => ({
        time: d.date as any,
        value: d.volume ?? 0,
        color: d.close >= d.open ? THEME.volBull : THEME.volBear,
      })) as HistogramData[],
    )

    const ts = chartRef.current.timeScale()
    if (data.length > 60) {
      const startIdx = data.length - 60
      ts.setVisibleRange({
        from: data[startIdx].date as any,
        to: data[data.length - 1].date as any,
      })
    } else {
      ts.fitContent()
    }
  }, [data])

  return <div ref={containerRef} className="w-full" style={{ height }} />
}
