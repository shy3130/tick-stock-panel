// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// useVersion 仅被 useUpdateCheck hook 使用; 打断 react-query 导入链, 单测只测命令式 API
vi.mock('@/lib/useSharedQueries', () => ({
  useVersion: () => ({ data: undefined }),
}))

import { checkForUpdate, compareVersion, getUpdateState } from './updateCheck'

const CACHE_KEY = 'update_check_cache'
const DAY = 24 * 60 * 60 * 1000

function seedCache(blob: Record<string, unknown>) {
  localStorage.setItem(CACHE_KEY, JSON.stringify(blob))
}

function readCacheRaw(): string | null {
  return localStorage.getItem(CACHE_KEY)
}

/** 模拟 GitHub Releases API 响应 (tag_name 可空以触发 latest.json 兜底) */
function mockFetchReleases(tagName: string) {
  return vi.fn(async (url: unknown) => {
    const u = String(url)
    if (u.includes('api.github.com')) {
      return { ok: true, json: async () => ({ tag_name: tagName, html_url: 'https://github.com/r/rel/1' }) }
    }
    if (u.includes('latest.json')) {
      return { ok: true, json: async () => ({ tag: tagName, notes_url: 'https://github.com/r/rel/1' }) }
    }
    return { ok: false, json: async () => ({}) }
  })
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('compareVersion', () => {
  it('按语义版本比较大小', () => {
    expect(compareVersion('0.3.2', '0.3.3')).toBe(-1)
    expect(compareVersion('0.4.0', '0.3.9')).toBe(1)
    expect(compareVersion('1.0.0', '1.0')).toBe(0)
  })

  it('容忍 v 前缀与非法输入 (按 0.0.0 处理)', () => {
    expect(compareVersion('0.3.2', 'v0.3.3')).toBe(-1)
    expect(compareVersion('v1.2.3', '1.2.3')).toBe(0)
    expect(compareVersion('', '0.0.1')).toBe(-1)
  })
})

describe('checkForUpdate', () => {
  it('同版本 24h 内命中缓存, 不发网络请求', async () => {
    seedCache({
      current: '0.3.2', latest: '0.3.3', url: 'https://github.com/r/rel/9',
      found: true, checkedAt: Date.now() - 1000,
    })
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await checkForUpdate('0.3.2')

    expect(fetchMock).not.toHaveBeenCalled()
    expect(getUpdateState().status).toBe('found')
    expect(getUpdateState().info?.latest).toBe('0.3.3')
    expect(getUpdateState().info?.url).toBe('https://github.com/r/rel/9')
  })

  it('缓存过期后重新请求并回写缓存', async () => {
    seedCache({
      current: '0.3.2', latest: '0.3.3', url: 'u',
      found: true, checkedAt: Date.now() - DAY - 1000,
    })
    vi.stubGlobal('fetch', mockFetchReleases('0.9.0'))

    await checkForUpdate('0.3.2')

    expect(getUpdateState().status).toBe('found')
    expect(getUpdateState().info?.latest).toBe('0.9.0')
    const cached = JSON.parse(readCacheRaw() ?? '{}')
    expect(cached).toMatchObject({ current: '0.3.2', latest: '0.9.0', found: true })
  })

  it('当前版本已是最新时状态为 latest', async () => {
    vi.stubGlobal('fetch', mockFetchReleases('v0.3.2'))

    await checkForUpdate('0.3.2')

    expect(getUpdateState().status).toBe('latest')
  })

  it('版本变化后缓存失效, 重新请求', async () => {
    seedCache({
      current: '0.3.1', latest: '0.3.2', url: 'u',
      found: true, checkedAt: Date.now(),
    })
    const fetchMock = mockFetchReleases('0.3.2')
    vi.stubGlobal('fetch', fetchMock)

    await checkForUpdate('0.3.2') // 本地版本已升级, 与缓存的 current 不一致

    expect(fetchMock).toHaveBeenCalled()
    expect(getUpdateState().status).toBe('latest')
  })

  it('force 绕过新鲜缓存立即刷新', async () => {
    seedCache({
      current: '0.3.2', latest: '0.9.9', url: 'u',
      found: true, checkedAt: Date.now(),
    })
    const fetchMock = mockFetchReleases('0.3.2')
    vi.stubGlobal('fetch', fetchMock)

    await checkForUpdate('0.3.2', { force: true })

    expect(fetchMock).toHaveBeenCalled()
    expect(getUpdateState().status).toBe('latest')
  })

  it('API 失败时状态 error 且不落缓存 (下次启动重试)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({}) })))

    await checkForUpdate('0.3.2')

    expect(getUpdateState().status).toBe('error')
    expect(readCacheRaw()).toBeNull()
  })

  it('GitHub API 字段为空时回退 latest.json 清单', async () => {
    const fetchMock = vi.fn(async (url: unknown) => {
      const u = String(url)
      if (u.includes('api.github.com')) {
        return { ok: true, json: async () => ({}) } // tag_name 缺失
      }
      if (u.includes('latest.json')) {
        return { ok: true, json: async () => ({ tag: '1.0.0', notes_url: 'https://github.com/r/notes' }) }
      }
      return { ok: false, json: async () => ({}) }
    })
    vi.stubGlobal('fetch', fetchMock)

    await checkForUpdate('0.3.2')

    expect(getUpdateState().status).toBe('found')
    expect(getUpdateState().info?.latest).toBe('1.0.0')
    expect(getUpdateState().info?.url).toBe('https://github.com/r/notes')
  })

  it('版本未知时空操作 (不请求不落缓存)', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await checkForUpdate('')

    expect(fetchMock).not.toHaveBeenCalled()
    expect(getUpdateState().status).not.toBe('checking')
  })
})
