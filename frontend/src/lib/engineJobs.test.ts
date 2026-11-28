// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { api } from './api'
import { QK } from './queryKeys'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

afterEach(() => vi.unstubAllGlobals())

test('research requests use the shared client and preserve their idempotency key', async () => {
  const fetch = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ job_id: 'abc' }), {
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fetch)
  const body = {
    idempotency_key: 'manual:1',
    nodes: [{ node_id: 'daily', run: 'run', snapshot: 'snapshot' }],
  }
  expect(await api.submitEngineJob(body)).toEqual({ job_id: 'abc' })
  expect(fetch.mock.calls[0][0]).toBe('/api/engine-jobs')
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body)
  expect(fetch.mock.calls[0][1].method).toBe('POST')
  expect(QK.engineJobs(20)).not.toEqual(QK.engineJobs(50))
  expect(QK.engineJobReport('a', 'daily')).not.toEqual(QK.engineJobReport('b', 'daily'))
})

test('structured rejection shows its readable message and preserves HTTP status', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: { code: 'DATA_CONFLICT', message: '原输入已改变，请创建新任务' },
        }),
        { status: 409 },
      ),
    ),
  )
  await expect(api.retryEngineJob('abc')).rejects.toMatchObject({
    message: '原输入已改变，请创建新任务',
    status: 409,
  })
})

test('preview and checked submission use the reviewed digest and inputs use an independent cache key', async () => {
  const fetch = vi
    .fn()
    .mockImplementation(async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetch)
  const body = {
    idempotency_key: 'checked:1',
    nodes: [{ node_id: 'daily', run: 'run', snapshot: 'snapshot' }],
  }
  const digest = 'a'.repeat(64)
  await api.engineInputs()
  await api.previewEngineJob(body)
  await api.submitEngineJob(body, digest)
  expect(fetch.mock.calls.map((call) => call[0])).toEqual([
    '/api/engine-jobs/inputs',
    '/api/engine-jobs/preview',
    `/api/engine-jobs?expected_plan_id=${digest}`,
  ])
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual(body)
  expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual(body)
  expect(QK.engineInputs).not.toEqual(QK.engineJobs())
  expect(QK.engineJobReport('a', 'first')).not.toEqual(QK.engineJobReport('a', 'second'))
})

test('schedule creation and activation stay separate and delivery cache keys include the schedule and limit', async () => {
  const fetch = vi.fn().mockImplementation(async () => new Response('{}', {
    headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetch)
  const body = {
    schedule_key: 'daily',
    trigger: { starts_at: '2026-11-18T01:00:00Z', interval_seconds: 86400 },
    template: { nodes: [{ node_id: 'daily', run: 'run', snapshot: 'snapshot' }] },
  }
  await api.createEngineSchedule(body)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body)
  await api.setEngineScheduleEnabled('a/b', true)
  await api.engineScheduleDeliveries('a/b', 20)
  expect(fetch.mock.calls[1][0]).toBe('/api/engine-jobs/schedules/a%2Fb/enabled')
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ enabled: true })
  expect(fetch.mock.calls[2][0]).toBe('/api/engine-jobs/schedules/a%2Fb/deliveries?limit=20')
  expect(QK.engineSchedules()).not.toEqual(QK.engineJobs())
  expect(QK.engineScheduleDeliveries('a', 20)).not.toEqual(QK.engineScheduleDeliveries('b', 20))
  expect(QK.engineScheduleDeliveries('a', 20)).not.toEqual(QK.engineScheduleDeliveries('a', 50))
  await api.engineScheduleCalendar('a/b')
  await api.engineCalendarPresets()
  expect(fetch.mock.calls[3][0]).toBe('/api/engine-jobs/schedules/a%2Fb/calendar')
  expect(fetch.mock.calls[4][0]).toBe('/api/engine-jobs/schedules/calendar-presets')
  expect(QK.engineScheduleCalendar('a')).not.toEqual(QK.engineScheduleCalendar('b'))
})
