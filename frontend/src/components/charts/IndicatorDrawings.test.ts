import { expect, it, vi } from 'vitest'
import { paintIndicatorDrawings } from './IndicatorDrawings'

it('uses native coordinates for sticks and positions wrapped text and arrows above the requested price', () => {
  const ctx = { save: vi.fn(), restore: vi.fn(), setLineDash: vi.fn(), fillRect: vi.fn(), strokeRect: vi.fn(),
    beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), closePath: vi.fn(), fill: vi.fn(), stroke: vi.fn(), fillText: vi.fn(), arc: vi.fn() }
  const timeToCoordinate = vi.fn(() => 40), priceToCoordinate = vi.fn((value: number) => 200 - value * 10)
  const chart = { timeScale: () => ({ options: () => ({ barSpacing: 12 }), timeToCoordinate }) }
  paintIndicatorDrawings(ctx as never, chart as never, { priceToCoordinate } as never, [
    { kind: 'stick', color: '#FF0000', width: 2, points: [{ time: '2026-01-01', value: 12, end_value: 8 }] },
    { kind: 'text', color: '#FF0000', above: true, points: [{ time: '2026-01-01', value: 12, text: '第一行&第二行' }] },
    { kind: 'icon', color: '#FF0000', above: true, points: [{ time: '2026-01-01', value: 12, icon: 1 }] },
    { kind: 'circles', color: '#FF0000', points: [{ time: '2026-01-01', value: 12 }] },
  ], 300, 200)
  expect(timeToCoordinate).toHaveBeenCalledWith('2026-01-01')
  expect(ctx.fillRect).toHaveBeenCalledWith(37, 80, 6, 40)
  expect(ctx.fillText.mock.calls).toEqual([['第一行', 40, 52], ['第二行', 40, 66]])
  expect(ctx.moveTo).toHaveBeenCalledWith(40, 66)
  expect(ctx.arc).toHaveBeenCalledWith(40, 80, 2, 0, Math.PI * 2)
})
