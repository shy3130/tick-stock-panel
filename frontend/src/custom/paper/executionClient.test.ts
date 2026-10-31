// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { executionApi } from './executionClient'
import { ApiError } from '@/lib/api'

afterEach(() => vi.unstubAllGlobals())
it('uses account-scoped extension requests and preserves authentication status', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '认证失败' }), { status: 401 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(executionApi.state('my-account')).rejects.toBeInstanceOf(ApiError)
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ detail: '认证失败' }), { status: 401 }))
  await expect(executionApi.state('my-account')).rejects.toThrow('未登录 (401)')
  expect(fetcher.mock.calls[0][0]).toBe('/api/custom/paper-execution/state?account=my-account')
})
it('keeps backend validation errors readable', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [{ msg: '不支持分钟成交' }] }), { status: 422 })))
  await expect(executionApi.preview('a', { config: { strategy_id: 'demo', minute_fill: true } })).rejects.toThrow('不支持分钟成交')
})
