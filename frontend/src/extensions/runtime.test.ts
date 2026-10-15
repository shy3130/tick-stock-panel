// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { loadRuntimePageExtensions, packToExtension, runtimeBundleUrl, type PagePackExtension, type RuntimePageMeta } from './runtime'
import type { FrontendExtensionModule } from './types'

function pack(over: Partial<PagePackExtension> = {}): PagePackExtension {
  return {
    id: 'demo.hello',
    apiVersion: 1,
    routes: [{ id: 'hello', path: '/demo-hello', mount: () => {} }],
    navigation: [{ id: 'hello', routeId: 'hello', label: '演示' }],
    ...over,
  }
}

function recorder() {
  const calls: Record<string, () => Promise<FrontendExtensionModule>>[] = []
  return {
    calls,
    loadInto: async (m: Record<string, () => Promise<FrontendExtensionModule>>) => { calls.push(m) },
  }
}

describe('packToExtension: 契约校验与转换', () => {
  it('合法清单转换为注册表扩展: mount 包装为组件、图标解析为白名单组件', () => {
    const ext = packToExtension(pack({ navigation: [{ id: 'hello', routeId: 'hello', label: '演示', icon: 'Zap' }] }))
    expect(ext.id).toBe('demo.hello')
    expect(ext.routes?.[0].path).toBe('/demo-hello')
    expect(typeof ext.routes?.[0].component).toBe('function')
    expect(ext.navigation?.[0].label).toBe('演示')
  })

  it('非法形态抛出可定位错误(版本/id/路由/mount)', () => {
    expect(() => packToExtension(null)).toThrow('default 导出')
    expect(() => packToExtension({ ...pack(), apiVersion: 2 } as unknown as PagePackExtension)).toThrow('apiVersion')
    expect(() => packToExtension({ ...pack(), id: 'no-namespace' })).toThrow('命名空间')
    expect(() => packToExtension({ ...pack(), routes: [] })).toThrow('routes')
    expect(() => packToExtension({ ...pack(), navigation: [] })).toThrow('navigation')
    expect(() => packToExtension({ ...pack(), routes: [{ id: 'x', path: 'relative', mount: () => {} }] })).toThrow('绝对路径')
    expect(() => packToExtension({ ...pack(), routes: [{ id: 'x', path: '/x' }] as never })).toThrow('mount')
  })
})

describe('loadRuntimePageExtensions', () => {
  it('只加载已启用页面插件, loader 键带 runtime: 前缀且完成契约转换', async () => {
    const rec = recorder()
    await loadRuntimePageExtensions({
      fetchList: async () => [
        { id: 'a', enabled: true },
        { id: 'b', enabled: false },
      ] as RuntimePageMeta[],
      importModule: async () => ({ default: pack() }),
      loadInto: rec.loadInto,
    })
    expect(Object.keys(rec.calls[0])).toEqual(['runtime:a'])
    const m = await rec.calls[0]['runtime:a']()
    expect(m.default.id).toBe('demo.hello') // 已是注册表扩展形态
    expect(runtimeBundleUrl('a')).toBe('/api/ext-pages/a/bundle.js')
  })

  it('契约非法的包在 loader 内抛错 → 由注册表记入 loadErrors 而非炸启动', async () => {
    const rec = recorder()
    await loadRuntimePageExtensions({
      fetchList: async () => [{ id: 'bad', enabled: true }],
      importModule: async () => ({ default: { id: 'x' } }),
      loadInto: rec.loadInto,
    })
    await expect(rec.calls[0]['runtime:bad']()).rejects.toThrow('apiVersion')
  })

  it('清单为空时不触发注册', async () => {
    const rec = recorder()
    await loadRuntimePageExtensions({ fetchList: async () => [], importModule: async () => ({ default: pack() }), loadInto: rec.loadInto })
    expect(rec.calls).toHaveLength(0)
  })

  it('fail-open: 后端不可达/401/超时均静默返回, 不抛错', async () => {
    const rec = recorder()
    await loadRuntimePageExtensions({ fetchList: async () => { throw new Error('backend down') }, loadInto: rec.loadInto })
    await loadRuntimePageExtensions({ fetchList: async () => { throw new Error('401 未登录') }, loadInto: rec.loadInto })
    await loadRuntimePageExtensions({
      fetchList: () => new Promise(() => {}), // 永不 resolve
      timeoutMs: 10,
      loadInto: rec.loadInto,
    })
    expect(rec.calls).toHaveLength(0)
  })

  it('注册器抛错也不拖垮启动 (兜底 catch)', async () => {
    await expect(loadRuntimePageExtensions({
      fetchList: async () => [{ id: 'a', enabled: true }],
      importModule: async () => ({ default: pack() }),
      loadInto: async () => { throw new Error('boom') },
    })).resolves.toBeUndefined()
  })

  it('清单里的畸形条目被跳过', async () => {
    const rec = recorder()
    await loadRuntimePageExtensions({
      fetchList: async () => [null, { enabled: true }, { id: 'ok', enabled: true }] as unknown as RuntimePageMeta[],
      importModule: async () => ({ default: pack() }),
      loadInto: rec.loadInto,
    })
    expect(Object.keys(rec.calls[0])).toEqual(['runtime:ok'])
  })
})
