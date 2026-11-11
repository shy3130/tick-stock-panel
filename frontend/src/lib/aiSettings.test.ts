// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { toast } from '@/components/Toast'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

it.each([404, 405])('explains a missing or rejected test route (HTTP %s)', async status => {
  const fetcher = vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ detail: status === 405 ? 'Method Not Allowed' : 'Not Found' }), { status },
  ))
  vi.stubGlobal('fetch', fetcher)
  await expect(api.testAiSettings({ provider: 'codex_cli', model: '' })).rejects.toMatchObject({
    status,
    message: expect.stringContaining('前后端版本一致'),
  })
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(toast).not.toHaveBeenCalled()
})

it.each(['<html>旧版页面</html>', '{}', 'null'])('explains invalid test responses without hiding the request failure', async body => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)))
  await expect(api.testAiSettings({ provider: 'codex_cli' })).rejects.toThrow('有效的后端响应')
})

it('keeps real CLI login failures distinct from panel routing failures', async () => {
  const result = { ok: false, error: 'Codex CLI 调用失败: 401 Unauthorized\n请执行 codex login' }
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(result)))
  vi.stubGlobal('fetch', fetcher)
  await expect(api.testAiSettings({ provider: 'codex_cli', model: '' }, { timeoutMs: 90_000 })).resolves.toEqual(result)
  expect(fetcher).toHaveBeenCalledWith('/api/settings/ai/test', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ provider: 'codex_cli', model: '' }),
  }))
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('preserves successful probes and authentication errors', async () => {
  const result = { ok: true, model: 'codex_cli', response: 'OK' }
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(result)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: '未登录或会话已过期' }), { status: 401 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(api.testAiSettings({ provider: 'codex_cli' })).resolves.toEqual(result)
  await expect(api.testAiSettings({ provider: 'codex_cli' })).rejects.toEqual(new ApiError('未登录或会话已过期', 401))
})
