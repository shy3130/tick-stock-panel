import { expect, it } from 'vitest'
import { buildIntradayModel, intradayTimeLabel, minuteTime, timeKey, validDailyRows } from './lightweight-model'
import { FULL_DAY_TIMES } from './intraday-chart'

it('minute canvas keys preserve Beijing wall clock independently of the host timezone', () => {
  expect(timeKey(minuteTime('2026-11-05 09:30:00'))).toBe('2026-11-05 09:30')
  expect(minuteTime('2026-11-05T09:30:00')).toBe(Date.UTC(2026, 10, 5, 9, 30) / 1000)
  expect(intradayTimeLabel(minuteTime('2026-11-05 13:00'))).toBe('2026-11-05 11:30/13:00')
  expect(intradayTimeLabel(minuteTime('2026-11-05 13:01'))).toBe('2026-11-05 13:01')
  expect(() => minuteTime('not-a-date')).toThrow()
})

it('sorts and upserts daily bars while rejecting invalid OHLC instead of plotting zeroes', () => {
  const a = { date: '2026-11-04', open: 1, high: 2, low: 1, close: 2 }
  expect(validDailyRows([{ ...a, date: '2026-11-05', close: NaN }, a, { ...a, close: 1.5 }])).toEqual([{ ...a, close: 1.5 }])
})

it('keeps the full session, gaps and per-day averages without inventing missing OHLC or amount', () => {
  const row = { datetime: '2026-11-04 09:30:00', open: null, high: 1, low: 1, close: 1, volume: 10, amount: null }
  const model = buildIntradayModel([
    { date: '2026-11-04', prev_close: 1, rows: [row, { ...row, datetime: '2026-11-04 09:32:00' }] },
    { date: '2026-11-05', prev_close: 1, rows: [{ ...row, datetime: '2026-11-05 09:30:00', amount: 2000 }] },
  ])
  expect(model.timeline).toHaveLength((FULL_DAY_TIMES.length - 1) * 2)
  expect(model.series.filter(s => s.type === 'Area')).toHaveLength(3)
  expect(model.points.get('2026-11-04 09:30')?.average).toBeNull()
  expect(model.points.get('2026-11-05 09:30')?.average).toBe(2)
  expect(model.latest?.row.open).toBeNull()
  expect(model.series.find(s => s.id === 'volume')?.data[0]).toMatchObject({ color: 'rgba(161,161,170,0.45)' })
})

it('does not relabel another date or include out-of-session bars in the average', () => {
  const row = { datetime: '2026-11-05 09:30:00', open: 1, high: 1, low: 1, close: 1, volume: 10, amount: 1000 }
  const model = buildIntradayModel([{ date: '2026-11-05', prev_close: 1, rows: [
    { ...row, datetime: '2026-11-04 09:31:00', close: 100 },
    { ...row, datetime: '2026-11-05 09:25:00', amount: 100000 }, row,
  ] }])
  expect(model.points.size).toBe(1)
  expect(model.latest?.average).toBe(1)
  expect(model.latest?.row.datetime).toBe(row.datetime)
})

it('shares the lunch boundary and keeps 11:30 to 13:01 continuous without inventing a missing 13:00 bar', () => {
  const rows = ['11:29', '11:30', '13:01', '13:02'].map((time, i) => ({
    datetime: `2026-11-05 ${time}:00`, open: 10, high: 11, low: 9, close: 10 + i / 10, volume: 10, amount: 10000,
  }))
  const model = buildIntradayModel([{ date: '2026-11-05', prev_close: 10, rows }])
  expect(model.timeline).toHaveLength(241)
  expect(model.timeline[120].time).toBe(minuteTime('2026-11-05 13:00'))
  expect(model.timeline[121].time).toBe(minuteTime('2026-11-05 13:01'))
  expect(model.series.filter(s => s.type === 'Area')).toHaveLength(1)
  expect(rows.map(r => r.datetime.slice(11, 16))).toEqual(['11:29', '11:30', '13:01', '13:02'])

  const missingMinute = buildIntradayModel([{ date: '2026-11-05', prev_close: 10, rows: rows.filter(r => !r.datetime.includes('13:01')) }])
  expect(missingMinute.series.filter(s => s.type === 'Area')).toHaveLength(2)
})

it('keeps both real boundary prices and aggregates their volume without duplicate times in a series', () => {
  const morning = { datetime: '2026-11-05 11:30:00', open: 9, high: 11, low: 9, close: 10, volume: 10, amount: 10000 }
  const afternoon = { ...morning, datetime: '2026-11-05 13:00:00', open: 11, high: 13, low: 11, close: 12, volume: 20, amount: 24000 }
  const model = buildIntradayModel([{ date: '2026-11-05', prev_close: 10, rows: [morning, afternoon] }])
  const boundary = minuteTime('2026-11-05 13:00')
  const volume = model.series.find(s => s.id === 'volume')!.data.filter(d => d.time === boundary)
  expect(volume).toEqual([expect.objectContaining({ value: 30 })])
  const point = model.points.get('2026-11-05 13:00')!
  expect(point.row).toMatchObject({ open: 9, high: 13, low: 9, close: 12, volume: 30, amount: 34000 })
  expect(point.average).toBeCloseTo(34000 / 3000)
  expect(point.sourceTimes).toEqual(['11:30', '13:00'])
  const prices = model.series.filter(s => s.type === 'Area').flatMap(s => s.data)
  expect(prices).toEqual([{ time: boundary, value: 10 }, { time: boundary, value: 12 }])
  for (const series of model.series) expect(new Set(series.data.map(d => d.time)).size).toBe(series.data.length)

  const startStamped = buildIntradayModel([{ date: '2026-11-05', prev_close: 10, rows: [afternoon] }])
  expect(startStamped.points.get('2026-11-05 13:00')?.row.datetime).toBe(afternoon.datetime)
  const unknown = buildIntradayModel([{ date: '2026-11-05', prev_close: 10, rows: [
    { ...morning, open: null }, { ...afternoon, amount: null },
  ] }]).points.get('2026-11-05 13:00')!
  expect(unknown.row.open).toBeNull()
  expect(unknown.row.amount).toBeNull()
  expect(unknown.average).toBeNull()
  const invalidVolume = buildIntradayModel([{ date: '2026-11-05', prev_close: 10, rows: [
    { ...morning, volume: -1 }, afternoon,
  ] }])
  expect(invalidVolume.series.find(s => s.id === 'volume')!.data.find(d => d.time === boundary)).toEqual({ time: boundary })
})
