import { expect, it, vi } from 'vitest'
import { candleBodyWidth, DailyVolumeSeries } from './DailyVolumeSeries'

it('matches candle pixel parity from narrow to wide zoom on standard and Retina displays', () => {
  expect([3, 6, 12, 20].map(spacing => candleBodyWidth(spacing, 1))).toEqual([3, 5, 9, 15])
  for (const ratio of [1, 1.5, 2, 3]) for (const spacing of [1, 3, 6, 12, 20, 40]) {
    const width = candleBodyWidth(spacing, ratio)
    expect(width).toBeGreaterThan(0)
    if (width >= 2) expect(width % 2).toBe(Math.floor(ratio) % 2)
  }
})

it('draws only visible nonempty volumes at their real height above zero, with candle-aligned widths', () => {
  const series = new DailyVolumeSeries()
  const fillRect = vi.fn(), ctx = { fillRect, fillStyle: '' }
  series.update({ barSpacing: 12, visibleRange: { from: 1, to: 3 }, conflationFactor: 1,
    bars: [10, 20, 0].map((value, i) => ({ x: 10 + i * 12, time: i, barColor: 'red', originalData: { time: '2026-11-05', value, color: 'green' } })) })
  series.renderer().draw({ useBitmapCoordinateSpace: (callback: any) => callback({ context: ctx, horizontalPixelRatio: 2, verticalPixelRatio: 2 }) } as never,
    (value: number) => 100 - value as never, false)
  const width = candleBodyWidth(12, 2)
  expect(fillRect).toHaveBeenCalledExactlyOnceWith(44 - Math.floor(width / 2), 160, width, 40)
  expect(ctx.fillStyle).toBe('green')
  expect(series.priceValueBuilder({ time: '2026-11-05', value: 20 })).toEqual([0, 20])
  expect(series.isWhitespace({ time: '2026-11-05' })).toBe(true)
  expect(series.isWhitespace({ time: '2026-11-05', value: NaN })).toBe(true)
  series.destroy(); fillRect.mockClear()
  series.renderer().draw({ useBitmapCoordinateSpace: vi.fn() } as never, () => null, false)
  expect(fillRect).not.toHaveBeenCalled()
})
