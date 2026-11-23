import { expect, it, vi } from 'vitest'
import type { PaneAttachedParameter, IPrimitivePaneRenderer, Time } from 'lightweight-charts'
import { minuteTime } from '@/lib/lightweight-model'
import { ChartTimeAxis, timeAxisTicks } from './ChartTimeAxis'

it('deduplicates shared series dates, orders trading days and marks year boundaries', () => {
  const ticks = timeAxisTicks(['2027-01-04', '2026-12-31', { year: 2026, month: 12, day: 31 }], false)
  expect(ticks.map(tick => tick.text)).toEqual(['12-31', '2027'])
  expect(ticks[1].priority).toBeGreaterThan(ticks[0].priority)
})

it('keeps Beijing session times, lunch and day boundaries without displaying arbitrary minute ticks', () => {
  const times = ['09:30', '09:31', '09:45', '10:00', '13:00', '15:00'].map(time => minuteTime(`2026-11-13 ${time}`))
  expect(timeAxisTicks(times, true).map(tick => tick.text)).toEqual(['09:30', '09:45', '10:00', '11:30/13:00', '15:00'])
  expect(timeAxisTicks([...times, minuteTime('2026-11-16 09:30')], true).map(tick => tick.text))
    .toEqual(['11-13', '09:45', '10:00', '11:30/13:00', '15:00', '11-16'])
})

function drawing(times: Time[], width = 300) {
  const scale = { getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: times.length - 1 })),
    timeToCoordinate: vi.fn((time: Time) => times.indexOf(time) * (width / Math.max(1, times.length - 1)) - 0.5) }
  const ctx = { save: vi.fn(), restore: vi.fn(), measureText: (text: string) => ({ width: text.length * 7 }),
    fillText: vi.fn(), fillRect: vi.fn(), font: '', textBaseline: '', fillStyle: '' }
  const axis = new ChartTimeAxis(), requestUpdate = vi.fn()
  axis.attached({ chart: { timeScale: () => scale }, requestUpdate } as unknown as PaneAttachedParameter<Time>)
  const draw = () => axis.paneViews()[0].renderer()!.draw({
    useMediaCoordinateSpace: (callback: (space: unknown) => void) => callback({ context: ctx, mediaSize: { width, height: 27 } }),
  } as unknown as Parameters<IPrimitivePaneRenderer['draw']>[0])
  return { axis, scale, ctx, draw, requestUpdate }
}

it('uses native positions and avoids clipped or overlapping labels in narrow panes after zooming', () => {
  const times: Time[] = Array.from({ length: 31 }, (_, i) => `2026-10-${String(i + 1).padStart(2, '0')}`)
  const { axis, scale, ctx, draw } = drawing(times, 200)
  axis.update(times, false, { text: '#888', crosshairLabelBg: '#333' })
  draw()
  const labels = ctx.fillText.mock.calls.map(([text, x]) => ({ left: x, right: x + text.length * 7 }))
    .sort((a, b) => a.left - b.left)
  expect(labels.length).toBeGreaterThan(1)
  labels.forEach((label, i) => {
    expect(label.left).toBeGreaterThanOrEqual(0)
    expect(label.right).toBeLessThanOrEqual(200)
    if (i) expect(label.left - labels[i - 1].right).toBeGreaterThanOrEqual(12)
  })
  scale.getVisibleLogicalRange.mockReturnValue({ from: 20, to: 25 })
  scale.timeToCoordinate.mockClear()
  draw()
  expect(scale.timeToCoordinate.mock.calls.every(([time]) => times.indexOf(time) >= 19 && times.indexOf(time) <= 26)).toBe(true)
})

it('renders and clears the exact hover time in the common axis and releases its chart on detach', () => {
  const time = minuteTime('2026-11-13 13:00')
  const { axis, scale, ctx, draw, requestUpdate } = drawing([time])
  scale.timeToCoordinate.mockReturnValue(100)
  axis.update([time], true, { text: '#777', crosshairLabelBg: '#52525B' })
  axis.setHoverTime(time)
  draw()
  expect(ctx.fillText).toHaveBeenCalledWith('2026-11-13 11:30/13:00', expect.any(Number), expect.any(Number))
  expect(ctx.fillRect).toHaveBeenCalledWith(expect.any(Number), 1, expect.any(Number), 25)
  ctx.fillText.mockClear()
  axis.setHoverTime(undefined)
  expect(requestUpdate).toHaveBeenCalledTimes(3)
  draw()
  expect(ctx.fillText).toHaveBeenCalledWith('11:30/13:00', expect.any(Number), expect.any(Number))
  expect(ctx.fillText).not.toHaveBeenCalledWith('2026-11-13 11:30/13:00', expect.any(Number), expect.any(Number))
  ctx.fillText.mockClear()
  axis.detached()
  axis.setHoverTime(time)
  draw()
  expect(ctx.fillText).not.toHaveBeenCalled()
  expect(requestUpdate).toHaveBeenCalledTimes(3)
})
