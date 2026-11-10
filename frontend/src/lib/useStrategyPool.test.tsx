// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

type Snapshot = import('./api').StrategyPoolSnapshot
type Meta = import('./api').StrategyLibraryMeta

// 后端 strategy-pool v2 的内存桩: 池是纯 ID 有序列表, 分组/归档/加入时间是
// 库级 meta 属性; 各操作直接改 server 状态并返回新快照, 与真实 API 的
// 「操作返回完整快照」契约一致。
const fake = vi.hoisted(() => {
  let seq = 0
  const server = {
    groups: [] as { id: string; name: string }[],
    pool: [] as string[],
    meta: {} as Record<string, Meta>,
  }
  const calls: string[] = []
  const ensureMeta = (id: string) => {
    if (!server.meta[id]) {
      server.meta[id] = { group: null, archived: false, archived_at: null, added_at: '2026-01-01T08:00:00' }
    }
    return server.meta[id]
  }
  const snap = (): Snapshot => ({
    groups: server.groups.map(g => ({ ...g })),
    pool: [...server.pool],
    meta: Object.fromEntries(Object.entries(server.meta).map(([id, m]) => [id, { ...m }])),
  })
  return {
    server, calls, snap, ensureMeta,
    reset() { seq = 0; server.groups = []; server.pool = []; server.meta = {}; calls.length = 0 },
    async setEntries(ids: string[]) { calls.push(`set:${ids.join(',')}`); server.pool = [...new Set(ids)]; return snap() },
    async add(id: string) { calls.push(`add:${id}`); ensureMeta(id); if (!server.pool.includes(id)) server.pool.push(id); return snap() },
    async remove(id: string) { calls.push(`remove:${id}`); server.pool = server.pool.filter(x => x !== id); return snap() },
    async archive(id: string) { calls.push(`archive:${id}`); ensureMeta(id).archived = true; ensureMeta(id).archived_at = '2026-11-08T10:00:00'; server.pool = server.pool.filter(x => x !== id); return snap() },
    async unarchive(id: string) { calls.push(`unarchive:${id}`); const m = server.meta[id]; if (m) { m.archived = false; m.archived_at = null } return snap() },
    async discard(id: string) { calls.push(`discard:${id}`); delete server.meta[id]; server.pool = server.pool.filter(x => x !== id); return snap() },
    async setGroup(id: string, group: string | null) { calls.push(`group:${id}:${group}`); ensureMeta(id).group = group; return snap() },
    async createGroup(name: string) { calls.push(`create-group:${name}`); seq += 1; const group = { id: `g${seq}`, name }; server.groups.push(group); return { ...snap(), group } },
    async deleteGroup(id: string) { calls.push(`delete-group:${id}`); server.groups = server.groups.filter(g => g.id !== id); for (const m of Object.values(server.meta)) { if (m.group === id) m.group = null } return snap() },
  }
})

vi.mock('@/lib/api', () => ({
  api: {
    strategyPoolSnapshot: async () => fake.snap(),
    strategyPoolSetEntries: fake.setEntries,
    strategyPoolAdd: fake.add,
    strategyPoolRemove: fake.remove,
    strategyPoolArchiveStrategy: fake.archive,
    strategyPoolUnarchiveStrategy: fake.unarchive,
    strategyPoolDiscardRecord: fake.discard,
    strategyPoolSetStrategyGroup: fake.setGroup,
    strategyPoolCreateGroup: fake.createGroup,
    strategyPoolRenameGroup: vi.fn(),
    strategyPoolReorderGroups: vi.fn(),
    strategyPoolDeleteGroup: fake.deleteGroup,
  },
}))

let host: HTMLDivElement
let root: Root | undefined
let current: ReturnType<typeof import('./useStrategyPool').useStrategyPool>
let client: QueryClient | undefined

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.useFakeTimers()
  localStorage.clear()
  fake.reset()
  host = document.createElement('div')
  document.body.appendChild(host)
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  host.remove()
  client?.clear()
  vi.clearAllTimers()
  vi.useRealTimers()
})

async function mount() {
  const { useStrategyPool } = await import('./useStrategyPool')
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client = queryClient
  function Probe() {
    current = useStrategyPool()
    return <output>{JSON.stringify(current.pool)}</output>
  }
  root = createRoot(host)
  await act(async () => root!.render(
    <QueryClientProvider client={queryClient}>
      <Probe />
    </QueryClientProvider>,
  ))
  // flush 查询 promise 链 (queryFn → 状态更新 → 重渲染)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
}

/** 动作是 fire-and-forget 的 void promise, 断言前 flush 掉 mutation 链 */
async function flush() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
}

it('loads the pool from the backend snapshot and exposes library meta', async () => {
  await fake.setEntries(['a', 'b'])
  fake.ensureMeta('a').group = 'g1'
  await mount()
  expect(current.pool).toEqual(['a', 'b'])
  expect(current.metaMap.get('a')).toMatchObject({ group: 'g1', archived: false })
  expect(current.groupNames.get('g1')).toBeUndefined() // 组本身未注册, 名称表只含已知组
})

it('addToPool calls the API and writes the legacy shadow for rollback', async () => {
  await mount()
  await act(async () => { current.addToPool('daily') })
  await flush()
  expect(current.pool).toEqual(['daily'])
  expect(fake.server.pool).toEqual(['daily'])
  expect(localStorage.getItem('strategy-pool')).toBe('["daily"]')
})

it.each([
  ['batch add', ['a', 'b', 'c']],
  ['batch remove', ['b']],
  ['clear', []],
] as const)('reorderPool supports %s as a complete membership replacement', async (_label, next) => {
  await fake.setEntries(['a', 'b'])
  await mount()
  await act(async () => { current.reorderPool([...next]) })
  await flush()
  expect(current.pool).toEqual(next)
  expect(fake.server.pool).toEqual(next)
})

it('archive flags library meta and removes the id from the pool; unarchive restores the library but not the pool', async () => {
  await fake.setEntries(['a', 'b'])
  await mount()
  await act(async () => { current.archiveStrategy('a') })
  await flush()
  expect(current.pool).toEqual(['b'])
  expect(current.archivedIds).toEqual(['a'])
  expect(current.metaMap.get('a')?.archived_at).toBeTruthy()

  await act(async () => { current.unarchiveStrategy('a') })
  await flush()
  expect(current.archivedIds).toEqual([])
  expect(current.metaMap.get('a')?.archived).toBe(false)
  expect(current.pool).toEqual(['b']) // 恢复只回库, 不自动回池
})

it('prune drops dead ids from the pool but keeps library records (including archived ones)', async () => {
  await fake.setEntries(['dead', 'valid'])
  // 档案库里同样躺着一个失效 id — prune 不应动库级 meta 记录
  await fake.archive('dead_arch')
  await mount()
  await act(async () => { current.prune(['valid']) })
  await flush()
  expect(current.pool).toEqual(['valid'])
  expect(fake.calls).toContain('set:valid')
  expect(current.archivedIds).toEqual(['dead_arch']) // 档案库记录不清
})

it('uploads a legacy local pool once when the backend snapshot is empty', async () => {
  localStorage.setItem('strategy-pool', '["local1","local2"]')
  await mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  expect(fake.calls).toContain('set:local1,local2')
  expect(current.pool).toEqual(['local1', 'local2'])
})

it('does not upload a legacy pool when the backend already has entries', async () => {
  await fake.setEntries(['server'])
  localStorage.setItem('strategy-pool', '["local"]')
  await mount()
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  expect(fake.calls).not.toContain('set:local')
  expect(current.pool).toEqual(['server'])
})

it('handleStrategyDeleted purges an archived-only record', async () => {
  await fake.archive('a') // a 只在档案库
  await mount()
  await act(async () => { current.handleStrategyDeleted('a') })
  await flush()
  expect(current.archivedIds).toEqual([])
})

it('handleStrategyDeleted purges a pool entry and its library record', async () => {
  await fake.setEntries(['b'])
  await mount()
  await act(async () => { current.handleStrategyDeleted('b') })
  await flush()
  expect(current.pool).toEqual([])
  expect(current.metaMap.has('b')).toBe(false)
})

it('group operations: create, assign, and delete clears member refs', async () => {
  await fake.setEntries(['x'])
  await mount()
  let groupId = ''
  await act(async () => { groupId = (await current.createGroup('趋势')).id })
  await flush()
  expect(current.groups.map(g => g.name)).toEqual(['趋势'])

  await act(async () => { current.setStrategyGroup('x', groupId) })
  await flush()
  expect(current.metaMap.get('x')?.group).toBe(groupId)

  await act(async () => { await current.deleteGroup(groupId) })
  await flush()
  expect(current.groups).toEqual([])
  expect(current.metaMap.get('x')?.group).toBeNull() // 组内策略转未分组
})
