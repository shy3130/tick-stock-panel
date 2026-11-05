import { expect, it, vi } from 'vitest'
import { ChartAnnotations, type AnnotationData } from './ChartAnnotations'

const dates = ['2026-11-03', '2026-11-04', '2026-11-05']
function setup(data: AnnotationData) {
  const painted: { text: string; x: number; y: number; color: string }[] = []
  const ctx = { fillStyle: '', save: vi.fn(), beginPath: vi.fn(), rect: vi.fn(), clip: vi.fn(), restore: vi.fn(),
    fillRect: vi.fn(), fillText: vi.fn(), setLineDash: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
    measureText: (text: string) => ({ width: text.length * 5 }),
  }
  ctx.fillText.mockImplementation((text: string, x: number, y: number) => { painted.push({ text, x, y, color: ctx.fillStyle }) })
  const requestUpdate = vi.fn()
  const priceToCoordinate = vi.fn((price: number) => 200 - price * 10)
  const annotation = new ChartAnnotations()
  annotation.attached({ requestUpdate, chart: { timeScale: () => ({
    logicalToCoordinate: (i: number) => i * 20,
    timeToCoordinate: (time: string) => dates.indexOf(time) * 20,
  }) }, series: { priceToCoordinate, getPane: () => ({ getHeight: () => 200 }) } } as never)
  annotation.update(data)
  const draw = (width = 300) => annotation.paneViews()[0].renderer()!.draw({ useMediaCoordinateSpace: (cb: any) => cb({ context: ctx, mediaSize: { width, height: 200 } }) } as never)
  return { annotation, ctx, draw, painted, requestUpdate, priceToCoordinate }
}

it('bounds price lines and range backgrounds by trading dates, ignoring expired and invalid annotations', () => {
  const { ctx, draw } = setup({ dates, ranges: [
    { start: dates[0], end: dates[1], label: '有效区间' },
    { start: '2026-10-01', end: '2026-10-30' }, { start: '2026-12-01', end: '2026-12-02' },
  ], priceLines: [
    { value: 10, start: dates[1], end: dates[2] },
    { value: 20, end: '2026-10-30' }, { value: NaN }, { value: 0 },
  ] })
  draw()
  expect(ctx.clip).toHaveBeenCalledOnce()
  expect(ctx.fillRect).toHaveBeenCalledExactlyOnceWith(0, 0, 20, 200)
  expect(ctx.moveTo).toHaveBeenCalledExactlyOnceWith(20, 100)
  expect(ctx.lineTo).toHaveBeenCalledExactlyOnceWith(40, 100)
})

it('right-aligns colored indicator values and wraps narrow headers without dropping values or overlapping the title', () => {
  const readouts = [{ text: 'DIF -0.001', color: '#FACC15' }, { text: 'DEA 0.000', color: '#8B5CF6' }, { text: 'MACD -0.002', color: '#2D9B65' }]
  const { annotation, painted, draw } = setup({ dates, title: 'MACD', readouts })
  for (const width of [300, 180]) {
    painted.length = 0
    draw(width)
    expect(painted[0]).toMatchObject({ text: 'MACD', x: 8, y: 14 })
    const values = painted.slice(1)
    expect(values.map(value => ({ text: value.text, color: value.color }))).toEqual(readouts)
    for (const value of values) {
      expect(value.x).toBeGreaterThanOrEqual(value.y === 14 ? 40 : 8)
      expect(value.x + value.text.length * 5).toBeLessThanOrEqual(width - 8)
    }
    for (const y of new Set(values.map(value => value.y))) {
      const last = values.filter(value => value.y === y).at(-1)!
      expect(last.x + last.text.length * 5).toBe(width - 8)
    }
    if (width === 180) expect(new Set(values.map(value => value.y)).size).toBe(2)
  }
  annotation.setCompact(true); painted.length = 0; draw()
  expect(painted).toHaveLength(4)
})

it('avoids overlapping volume labels and hides them in compact mode without removing the pane title', () => {
  const { annotation, ctx, draw } = setup({ dates, title: '成交量', labels: dates.map(time => ({ time, value: 10, text: '1.5x' })) })
  draw()
  expect(ctx.fillText.mock.calls.filter(call => call[0] === '1.5x')).toHaveLength(2)
  ctx.fillText.mockClear()
  annotation.setCompact(true); draw()
  expect(ctx.fillText).toHaveBeenCalledExactlyOnceWith('成交量', 8, 14)
})

it('drops its chart references when detached and stops requesting draws', () => {
  const { annotation, ctx, draw, requestUpdate } = setup({ dates })
  requestUpdate.mockClear()
  annotation.detached(); annotation.update({ dates, title: '旧标的' }); draw()
  expect(requestUpdate).not.toHaveBeenCalled()
  expect(ctx.save).not.toHaveBeenCalled()
})

it('keeps all day separators but skips overlapping date labels', () => {
  const { ctx, draw } = setup({ dates, verticals: dates.map(time => ({ time, label: time.slice(5) })) })
  draw()
  expect(ctx.stroke).toHaveBeenCalledTimes(3)
  expect(ctx.fillText).toHaveBeenCalledTimes(2)
})

it('pins the previous-close number to its dashed line on the outer price axis and follows scale changes', () => {
  const { annotation, ctx, draw, priceToCoordinate } = setup({ dates, priceLines: [
    { value: 10, color: '#A1A1AA', axisLabel: true }, { value: 12, label: '价格提醒' },
    { value: NaN, axisLabel: true }, { value: 0, axisLabel: true },
    { value: 11, end: '2026-10-30', axisLabel: true },
  ] })
  draw()
  expect(ctx.fillText.mock.calls.map(call => call[0])).toEqual(['价格提醒'])
  const views = annotation.priceAxisViews()
  expect(views).toHaveLength(1)
  expect(annotation.priceAxisViews()).toBe(views)
  expect(views[0].text()).toBe('10.00')
  expect(views[0].fixedCoordinate?.()).toBe(ctx.moveTo.mock.calls[0][1])
  priceToCoordinate.mockReturnValue(75)
  ctx.moveTo.mockClear(); draw()
  expect(views[0].fixedCoordinate?.()).toBe(75)
  expect(views[0].fixedCoordinate?.()).toBe(ctx.moveTo.mock.calls[0][1])
  annotation.update({ dates, priceLines: [{ value: 15, axisLabel: true }] })
  expect(annotation.priceAxisViews()[0].text()).toBe('15.00')
  annotation.update({ dates })
  expect(annotation.priceAxisViews()).toHaveLength(0)
  annotation.update({ dates, priceLines: [{ value: 15, axisLabel: true }] })
  annotation.detached()
  expect(annotation.priceAxisViews()).toHaveLength(0)
})

it('keeps limit-price labels fully visible at the top and bottom without adding text inside the plot', () => {
  const { annotation, ctx, draw, priceToCoordinate } = setup({ dates, priceLines: [
    { value: 20, color: '#C74040', axisLabel: true },
    { value: 1, color: '#2D9B65', axisLabel: true }, { value: 10, axisLabel: true },
  ] })
  priceToCoordinate.mockImplementation(price => price === 20 ? 0 : price === 1 ? 199 : 100)
  draw()
  const views = annotation.priceAxisViews()
  expect(ctx.fillText).not.toHaveBeenCalled()
  expect(views.map(view => view.text())).toEqual(['20.00', '1.00', '10.00'])
  expect(views.map(view => view.fixedCoordinate?.())).toEqual([10, 190, 100])
  expect(views[0].textColor()).toBe('#C74040')
  expect(views.every(view => view.backColor() === 'transparent')).toBe(true)
  priceToCoordinate.mockReturnValue(-10)
  expect(views.every(view => view.visible?.() === false)).toBe(true)
})

it('supports zero and negative percentage ticks and keeps the center and official prices over nearby ordinary ticks', () => {
  const { annotation, priceToCoordinate } = setup({ dates: [], axisTicks: [
    { value: -2, text: '-2.00%' }, { value: 0, text: '0.00%', priority: 3 }, { value: 2, text: '+2.00%' },
  ] })
  priceToCoordinate.mockImplementation(value => 100 - value * 40)
  expect(annotation.priceAxisViews().every(view => view.visible?.())).toBe(true)
  expect(annotation.priceAxisViews().map(view => view.fixedCoordinate?.())).toEqual([180, 100, 20])
  annotation.setHoverY(101)
  expect(annotation.priceAxisViews().map(view => view.visible?.())).toEqual([true, false, true])
  annotation.setHoverY()
  annotation.update({ dates: [], axisTicks: [{ value: 10, text: '10.00', priority: 3 }, { value: 10.1, text: '10.10' }],
    priceLines: [{ value: 10.2, axisLabel: true }] })
  priceToCoordinate.mockImplementation(value => 200 - value * 10)
  expect(annotation.priceAxisViews().map(view => view.visible?.())).toEqual([true, false, false])
})
