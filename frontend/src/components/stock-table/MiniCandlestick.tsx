/** 迷你蜡烛图（自选/策略列表共享）。
 *
 * React.memo: rows 引用由上游收敛 (Watchlist 的 klineData 出口 reuseUnchangedSeries,
 * 见 lib/series-identity) — 实时行情 tick 中数据未变的行跳过整棵 SVG 重渲染。
 * 调用方传 rows 时必须用共享空数组常量 (EMPTY_ROWS), 不要 `?? []` 新建引用。 */
import { memo } from 'react'
import type { KlineRow } from '@/lib/api'

export const MiniCandlestick = memo(function MiniCandlestick({ rows, width = 100, height = 80, volume = false }: { rows: KlineRow[]; width?: number; height?: number; volume?: boolean }) {
  // 空数据：返回等尺寸占位（不画内容），保证 kline 加载前后单元格尺寸一致、不闪烁
  if (!rows || rows.length === 0) {
    return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block" aria-label="加载中" />
  }

  const BULL = '#C74040'
  const BEAR = '#2D9B65'
  const NEUTRAL = '#A1A1AA'
  const LINE_PREV_CLOSE = '#7A7A85'

  const W = width
  const H = height
  const padY = 2
  const n = rows.length
  const barW = W / n
  const bodyW = Math.max(barW * 0.55, 2)

  let hi = -Infinity, lo = Infinity
  for (const r of rows) {
    if (r.high > hi) hi = r.high
    if (r.low < lo) lo = r.low
  }
  const range = hi - lo || 1

  // 底部量带 (与 MiniIntraday 分时副图同构): 价格区止于 ~64%, 下方独立量带。
  //
  // 阈值与 MiniIntraday 对齐: 那边只要有量就画 (H=32 也有 3.5px 带)。这里曾用
  // 「带宽 ≥12px」守卫, 导致同样开着量带、同样 40px 高度时, 日K不画而分时不画不出
  // 差异 —— 用户看到的是"开关打开但日k没量柱"。改为最小可读下限 3px (低于此柱高
  // 不足 1px, 画了反而是噪点), 与分时的行为一致。
  const volTopCandidate = Math.round(H * 0.64) + 2
  const volBandH = (H - padY) - volTopCandidate
  const volActive = volume && rows.some(r => (r.volume ?? 0) > 0) && volBandH >= 3
  const priceBottom = volActive ? Math.round(H * 0.64) : H - padY
  const bottomY = H - padY

  const yScale = (v: number) => padY + (1 - (v - lo) / range) * (priceBottom - padY)

  const rects: React.ReactNode[] = []
  const wicks: React.ReactNode[] = []
  const volRects: React.ReactNode[] = []

  let vmax = 0
  if (volActive) {
    for (const r of rows) {
      if ((r.volume ?? 0) > vmax) vmax = r.volume ?? 0
    }
    vmax = vmax || 1
  }

  for (let i = 0; i < n; i++) {
    const r = rows[i]
    const x = i * barW + barW / 2

    // 涨跌判断: open !== close 用实体方向, 一字板用前日收盘计算涨跌
    let color: string
    if (r.close > r.open) {
      color = BULL
    } else if (r.close < r.open) {
      color = BEAR
    } else {
      // 一字板: open === close, 用前一日收盘价判断涨跌方向
      const prevClose = i > 0 ? rows[i - 1].close : null
      if (prevClose && prevClose > 0 && r.close !== prevClose) {
        color = r.close > prevClose ? BULL : BEAR
      } else {
        color = NEUTRAL
      }
    }

    wicks.push(
      <line
        key={`w${i}`}
        x1={x} y1={yScale(r.high)} x2={x} y2={yScale(r.low)}
        stroke={color} strokeWidth={1}
      />
    )

    const top = yScale(Math.max(r.open, r.close))
    const bot = yScale(Math.min(r.open, r.close))
    const bodyH = Math.max(bot - top, 1)

    rects.push(
      <rect
        key={`c${i}`}
        x={x - bodyW / 2} y={top}
        width={bodyW} height={bodyH}
        fill={color}
      />
    )

    // 量柱: 宽与蜡烛实体同款 bodyW (防亚像素发丝), 颜色随该蜡烛实体色 (含一字板
    // 前收规则), 按窗口最大量归一; 无量数据的 K 不画柱
    if (volActive && (r.volume ?? 0) > 0) {
      const vh = ((r.volume ?? 0) / vmax) * volBandH
      volRects.push(
        <rect
          key={`v${i}`}
          x={x - bodyW / 2} y={bottomY - vh}
          width={bodyW} height={vh}
          fill={color} opacity={0.4}
        />
      )
    }
  }

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block">
      {wicks}
      {rects}
      {volRects}
      {volActive && (
        <line
          x1={0} y1={volTopCandidate - 1} x2={W} y2={volTopCandidate - 1}
          stroke={LINE_PREV_CLOSE} strokeWidth={0.5} opacity={0.25}
        />
      )}
    </svg>
  )
})
