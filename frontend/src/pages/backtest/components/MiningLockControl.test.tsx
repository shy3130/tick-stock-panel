// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type MiningLockStatus } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MiningLockControl } from './MiningLockControl'

vi.mock('@/lib/api', () => ({ api: {
  miningLockStatus: vi.fn(), miningRecoverLock: vi.fn(), miningForceReleaseLock: vi.fn(),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const status = (overrides: Partial<MiningLockStatus> = {}): MiningLockStatus => ({
  held: false, mine: false, holder: null, holder_alive: false, pid_consistent: true,
  active_runs: 0, store_idle_seconds: null, stale_after_seconds: 600,
  force_releasable: false, reason: '锁未被持有', ...overrides,
})
const owned = () => status({ held: true, mine: true, holder: { pid: 123 }, holder_alive: true })
const button = (text: string) => Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes(text))
const flush = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) }) }

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.miningLockStatus).mockResolvedValue(status())
  vi.mocked(api.miningRecoverLock).mockResolvedValue(owned())
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><MiningLockControl /></QueryClientProvider>))
  await flush()
}

it('健康状态保留检查入口，不显示占用警告或强制停止', async () => {
  vi.mocked(api.miningLockStatus).mockResolvedValue(owned())
  await render()
  expect(host.textContent).toContain('挖掘已就绪')
  expect(button('检查并恢复挖掘')?.disabled).toBe(false)
  expect(button('强制停止')).toBeUndefined()
})

it('空闲锁点击恢复后立即更新状态并失效任务缓存', async () => {
  client.setQueryData(QK.miningRuns, { items: [] })
  client.setQueryData(QK.miningRun('orphan'), { status: 'running' })
  await render()
  expect(host.textContent).toContain('锁已空闲')
  await act(async () => button('检查并恢复挖掘')!.click())
  await flush()
  expect(api.miningRecoverLock).toHaveBeenCalledOnce()
  expect(api.miningForceReleaseLock).not.toHaveBeenCalled()
  expect(host.textContent).toContain('挖掘已就绪')
  expect(client.getQueryState(QK.miningRuns)?.isInvalidated).toBe(true)
  expect(client.getQueryState(QK.miningRun('orphan'))?.isInvalidated).toBe(true)
})

it('未知持有者仍能检查，不武断提示旧格式，不提供强制停止', async () => {
  const unknown = status({ held: true, reason: '暂时无法确认占用进程，请正常退出其他实例后再检查' })
  vi.mocked(api.miningLockStatus).mockResolvedValue(unknown)
  vi.mocked(api.miningRecoverLock).mockResolvedValue(unknown)
  await render()
  await act(async () => button('检查并恢复挖掘')!.click())
  await flush()
  expect(host.textContent).toContain('暂时无法确认占用进程')
  expect(host.textContent).not.toContain('旧格式')
  expect(button('强制停止')).toBeUndefined()
  expect(api.miningForceReleaseLock).not.toHaveBeenCalled()
})

it('活跃任务显示拒绝原因，检查不会终止占用者', async () => {
  const live = status({ held: true, holder: { pid: 321 }, active_runs: 1, reason: '仍有 1 个进行中的挖掘运行' })
  vi.mocked(api.miningLockStatus).mockResolvedValue(live)
  vi.mocked(api.miningRecoverLock).mockResolvedValue(live)
  await render()
  await act(async () => button('检查并恢复挖掘')!.click())
  await flush()
  expect(host.textContent).toContain('1 个进行中的挖掘运行')
  expect(button('强制停止')).toBeUndefined()
  expect(api.miningForceReleaseLock).not.toHaveBeenCalled()
})

it('强制停止需另行确认，并提交确认时的 PID', async () => {
  vi.mocked(api.miningLockStatus).mockResolvedValue(status({ held: true, holder: { pid: 321 }, force_releasable: true }))
  vi.mocked(api.miningForceReleaseLock).mockResolvedValue({ released: true, terminated_pid: 321, reacquired: true })
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  await render()
  await act(async () => button('强制停止')!.click())
  expect(api.miningForceReleaseLock).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  vi.mocked(api.miningLockStatus).mockResolvedValue(owned())
  await act(async () => button('强制停止')!.click())
  await flush()
  expect(api.miningForceReleaseLock).toHaveBeenCalledWith(321, expect.anything())
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('321'))
  expect(host.textContent).toContain('挖掘已就绪')
})

it('检查过程中禁止重复点击，错误原因可见且可以重试', async () => {
  let reject!: (reason: Error) => void
  vi.mocked(api.miningRecoverLock).mockImplementation(() => new Promise((_, fail) => { reject = fail }))
  await render()
  await act(async () => button('检查并恢复挖掘')!.click())
  await flush()
  expect(button('正在检查并恢复')?.disabled).toBe(true)
  await act(async () => reject(new Error('无法获取挖掘锁，请检查数据目录权限后重试')))
  await flush()
  expect(host.textContent).toContain('数据目录权限')
  expect(button('检查并恢复挖掘')?.disabled).toBe(false)
  vi.mocked(api.miningRecoverLock).mockResolvedValue(owned())
  await act(async () => button('检查并恢复挖掘')!.click())
  await flush()
  expect(host.textContent).toContain('挖掘已就绪')
})

it('首次查询失败也保留恢复按钮', async () => {
  vi.mocked(api.miningLockStatus).mockRejectedValue(new Error('连接暂时不可用'))
  await render()
  expect(host.textContent).toContain('连接暂时不可用')
  expect(button('检查并恢复挖掘')?.disabled).toBe(false)
  await act(async () => button('检查并恢复挖掘')!.click())
  await flush()
  expect(host.textContent).toContain('挖掘已就绪')
})
