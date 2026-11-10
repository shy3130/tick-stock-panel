// @vitest-environment jsdom
// v2 语义: 弹窗为「实时操作模型」— 左侧策略库可排序/分组/归档 (无草稿, 无保存
// 按钮), 勾选即时入池。此处维护一个状态化的 StrategyPool 桩, 操作改本地状态
// 并记录调用, 让弹窗像接上真后端一样重渲染。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StrategyPoolDialog } from './StrategyPoolDialog'
import type { StrategyDetail, StrategyLibraryMeta, StrategyPoolGroup } from '@/lib/api'
import type { StrategyPool } from '@/lib/useStrategyPool'

const mockStrategyList = vi.fn()
const mockStrategyPublish = vi.fn(async (id: string) => ({ ok: true, strategy_id: id }))
vi.mock('@/lib/api', () => ({
  api: {
    strategyList: (...args: unknown[]) => mockStrategyList(...args),
    strategyPublish: (id: string) => mockStrategyPublish(id),
  },
}))

function mkStrategy(p: Partial<StrategyDetail> & Pick<StrategyDetail, 'id' | 'name' | 'source'>): StrategyDetail {
  return {
    description: '', tags: [], research_only: false,
    execution_backend: 'polars_expr', asset_types: ['stock'], timeframes: ['1d'],
    version: '1', basic_filter: {}, params: [], params_defaults: {},
    scoring: {}, scoring_directions: {}, entry_signals: [], exit_signals: [],
    minute_exit_trigger_supported_signals: [], stop_loss: null, take_profit: null,
    trailing_stop: null, trailing_take_profit_activate: null,
    trailing_take_profit_drawdown: null, max_hold_days: null,
    order_by: '', descending: true, limit: 50,
    ...p,
  }
}

const aiDraft = mkStrategy({ id: 'ai_breakout_v1', name: 'AI突破', source: 'ai', research_only: true })
const builtinResearch = mkStrategy({ id: 'factor_rank_research', name: '因子排名研究', source: 'custom', research_only: true })
const builtinPublic = mkStrategy({ id: 'trend_follow', name: '趋势跟随', source: 'custom' })
const compositePublic = mkStrategy({ id: 'composite_mix_v1', name: '混合叠加', source: 'composite' })

const mkMeta = (p: Partial<StrategyLibraryMeta> = {}): StrategyLibraryMeta => ({
  group: null, archived: false, archived_at: null, added_at: '2026-01-01T08:00:00', ...p,
})

interface HarnessOptions {
  ids?: string[]
  meta?: Record<string, StrategyLibraryMeta>
  groups?: StrategyPoolGroup[]
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)
let calls: string[] = []
const onAdded = vi.fn()
const onClose = vi.fn()

/** 状态化 StrategyPool 桩: 每个操作更新本地状态 (触发重渲染) 并记录调用序列 */
function Harness({ ids: initialIds, meta: initialMeta, groups: initialGroups }: HarnessOptions) {
  const [ids, setIds] = useState(initialIds ?? [])
  const [meta, setMeta] = useState(initialMeta ?? {})
  const [groups, setGroups] = useState(initialGroups ?? [])
  const setMetaOf = (id: string, patch: (m: StrategyLibraryMeta) => StrategyLibraryMeta) =>
    setMeta(prev => ({ ...prev, [id]: patch(prev[id] ?? mkMeta()) }))
  const pool: StrategyPool = {
    pool: ids,
    groups,
    groupNames: new Map(groups.map(g => [g.id, g.name])),
    metaMap: new Map(Object.entries(meta)),
    archivedIds: Object.entries(meta).filter(([, m]) => m.archived).map(([id]) => id),
    isLoading: false, isError: false,
    addToPool: (id: string) => { calls.push(`add:${id}`); setIds(prev => prev.includes(id) ? prev : [...prev, id]) },
    removeFromPool: (id: string) => { calls.push(`remove:${id}`); setIds(prev => prev.filter(x => x !== id)) },
    reorderPool: (next: string[]) => { calls.push(`reorder:${next.join(',')}`); setIds(next) },
    archiveStrategy: (id: string) => {
      calls.push(`archive:${id}`)
      setMetaOf(id, m => ({ ...m, archived: true, archived_at: '2026-11-08T10:00:00' }))
      setIds(prev => prev.filter(x => x !== id))
    },
    unarchiveStrategy: (id: string) => {
      calls.push(`unarchive:${id}`)
      setMetaOf(id, m => ({ ...m, archived: false, archived_at: null }))
    },
    discardRecord: (id: string) => {
      calls.push(`discard:${id}`)
      setMeta(prev => { const next = { ...prev }; delete next[id]; return next })
    },
    setStrategyGroup: (id: string, group: string | null) => { calls.push(`group:${id}:${group}`); setMetaOf(id, m => ({ ...m, group })) },
    createGroup: async (name: string) => {
      calls.push(`create-group:${name}`)
      const group = { id: `g${groups.length + 1}`, name }
      setGroups(prev => [...prev, group])
      return group
    },
    renameGroup: async (id: string, name: string) => {
      calls.push(`rename-group:${id}:${name}`)
      setGroups(prev => prev.map(g => g.id === id ? { ...g, name } : g))
    },
    reorderGroups: async (orderedIds: string[]) => {
      calls.push(`reorder-groups:${orderedIds.join(',')}`)
      setGroups(orderedIds.map(id => groups.find(g => g.id === id)!))
    },
    deleteGroup: async (id: string) => {
      calls.push(`delete-group:${id}`)
      setGroups(prev => prev.filter(g => g.id !== id))
      setMeta(prev => Object.fromEntries(Object.entries(prev).map(([key, m]) => [key, m.group === id ? { ...m, group: null } : m])))
    },
    prune: vi.fn(), handleStrategyDeleted: vi.fn(),
    isInPool: (id: string) => ids.includes(id),
  }
  return <StrategyPoolDialog pool={pool} onAdded={onAdded} onClose={onClose} />
}

async function render(options: HarnessOptions = {}) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  calls = []
  onAdded.mockClear()
  onClose.mockClear()
  root = createRoot(container)
  await act(async () => { root!.render(<Harness {...options} />) })
}

function clickTab(label: string) {
  const tab = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent?.startsWith(label))
  expect(tab).toBeTruthy()
  act(() => { tab!.click() })
}

const clickAiTab = () => clickTab('AI')
const button = (label: string) => [...container.querySelectorAll('button')].find(el => el.textContent?.trim() === label)!
const choice = (name: string) => container.querySelector<HTMLInputElement>(`[aria-label="选择策略：${name}"]`)!
const library = () => container.querySelector('[aria-label="策略池"]')!
const archive = () => container.querySelector('[aria-label="档案库"]')!
/** 分组 chip (role=group): 点名称区筛选, 内部勾选框整组入池/出池 */
const groupChip = (label: string) => container.querySelector<HTMLDivElement>(`[aria-label="分组：${label}"]`)!
const groupChipBox = (label: string) => groupChip(label).querySelector<HTMLInputElement>(`[aria-label="${label}全选反选"]`)!
const selected = () => container.querySelector('[aria-label="已选策略"]')!
const groupCount = (label: string) => Number(groupChip(label).lastElementChild!.textContent)
/** 左侧策略库行的显示顺序 (按勾选框出现顺序) */
const libraryOrder = () =>
  [...library().querySelectorAll<HTMLInputElement>('[aria-label^="选择策略："]')]
    .map(el => el.getAttribute('aria-label')!.slice('选择策略：'.length))
const selectOf = (label: string) => container.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!
const setSelect = (el: HTMLSelectElement, value: string) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!
  act(() => { setter.call(el, value); el.dispatchEvent(new Event('change', { bubbles: true })) })
}

beforeEach(() => {
  mockStrategyList.mockReset()
  mockStrategyList.mockResolvedValue({ strategies: [aiDraft, builtinResearch, builtinPublic, compositePublic] })
  mockStrategyPublish.mockClear()
  localStorage.clear()
})
afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
})

it('AI tab lists only ai-source drafts with a publish button', async () => {
  await render()
  clickAiTab()

  expect(library().textContent).toContain('AI突破')
  const publishBtn = Array.from(library().querySelectorAll('button'))
    .find(b => b.textContent?.includes('发布'))
  expect(publishBtn).toBeTruthy()

  await act(async () => { publishBtn!.click() })
  expect(mockStrategyPublish).toHaveBeenCalledWith('ai_breakout_v1')
})

it('builtin research template is not offered anywhere in the pool', async () => {
  await render()

  // 默认「全部」分类: research_only 项不进待选, 模板应完全不出现
  expect(library().textContent).not.toContain('因子排名研究')
  expect(library().textContent).toContain('趋势跟随')

  clickAiTab()
  // 草稿分区同样不列非 AI 来源的 research 项
  expect(library().textContent).not.toContain('因子排名研究')
})

it('composite strategies have their own tab and are excluded from custom tab', async () => {
  await render()

  clickTab('叠加')
  expect(libraryOrder()).toEqual(['混合叠加'])

  clickTab('自定义')
  expect(libraryOrder()).toEqual(['趋势跟随'])

  clickTab('全部')
  expect(libraryOrder()).toEqual(['趋势跟随', '混合叠加'])
})

it('toggling a checkbox takes effect immediately — no save button in the live model', async () => {
  await render()
  // v2 是实时模型: 不存在「保存策略池」草稿按钮
  expect([...container.querySelectorAll('button')].some(el => el.textContent?.includes('保存'))).toBe(false)

  act(() => { choice('趋势跟随').click() })
  expect(calls).toEqual(['add:trend_follow'])
  expect(onAdded).toHaveBeenCalledWith(['trend_follow'])
  expect(choice('趋势跟随').checked).toBe(true)
  expect(selected().textContent).toContain('趋势跟随')

  act(() => { choice('趋势跟随').click() })
  expect(calls).toEqual(['add:trend_follow', 'remove:trend_follow'])
  expect(choice('趋势跟随').checked).toBe(false)
  expect(selected().textContent).not.toContain('趋势跟随')
})

it('searches by ID and combines source and timeframe filtering', async () => {
  mockStrategyList.mockResolvedValue({ strategies: [builtinPublic, compositePublic,
    mkStrategy({ id: 'custom_minute', name: '分钟突破', source: 'custom', timeframes: ['1m'] })] })
  await render()
  clickTab('自定义')
  const input = container.querySelector<HTMLInputElement>('[aria-label="搜索策略"]')!
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => { setter.call(input, 'custom_minute'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(choice('分钟突破')).toBeTruthy()
  expect(choice('趋势跟随')).toBeNull()
  setSelect(selectOf('策略周期'), '1d')
  expect(choice('分钟突破')).toBeNull()
  expect(library().textContent).toContain('没有符合条件的策略')
})

it('reorders the selected list live via move buttons', async () => {
  await render({ ids: ['trend_follow', 'composite_mix_v1'] })
  const move = container.querySelector<HTMLButtonElement>('[aria-label="下移：趋势跟随"]')!
  act(() => { move.click() })
  expect(calls).toContain('reorder:composite_mix_v1,trend_follow')
  expect([...selected().querySelectorAll('[data-strategy-id]')].map(el => el.getAttribute('data-strategy-id')))
    .toEqual(['composite_mix_v1', 'trend_follow'])
})

it('clears and undoes clearing the selection', async () => {
  await render({ ids: ['trend_follow'] })
  act(() => { button('清空所选').click() })
  expect(calls).toContain('reorder:')
  expect(selected().textContent).not.toContain('趋势跟随')
  act(() => { button('撤销清空').click() })
  expect(calls).toContain('reorder:trend_follow')
  expect(choice('趋势跟随').checked).toBe(true)
})

it('load failure shows the error and retry restores the list', async () => {
  mockStrategyList.mockRejectedValueOnce(new Error('连接中断'))
  await render({ ids: ['trend_follow'] })
  expect(container.textContent).toContain('策略列表加载失败')
  expect(choice('趋势跟随')).toBeNull() // 列表未加载, 库行不渲染
  await act(async () => { button('重试').click() })
  expect(choice('趋势跟随').checked).toBe(true)
})

it('flags dead pool ids in the selected list without blocking anything', async () => {
  await render({ ids: ['deleted_strategy'] })
  expect(selected().textContent).toContain('失效策略')
  expect(container.textContent).not.toContain('保存') // 实时模型, 无保存闸
})

it('archives from the library row, hides it from the library, and restores from the archive pane without re-pooling', async () => {
  await render({ ids: ['trend_follow', 'composite_mix_v1'] })
  act(() => { container.querySelector<HTMLButtonElement>('[aria-label="归档：趋势跟随"]')!.click() })

  expect(calls).toEqual(['archive:trend_follow'])
  // 移出池且不再出现在策略池 (左侧库级归档)
  expect(libraryOrder()).toEqual(['混合叠加'])
  expect(choice('趋势跟随')).toBeNull()
  expect([...selected().querySelectorAll('[data-strategy-id]')]).toHaveLength(1)

  // 档案库视图可见且可恢复
  clickTab('档案库')
  expect(archive().textContent).toContain('趋势跟随')
  expect(archive().textContent).toContain('归档于 11-08 10:00')
  const restore = [...archive().querySelectorAll('button')].find(b => b.textContent?.includes('恢复'))!
  act(() => { restore.click() })

  expect(calls).toEqual(['archive:trend_follow', 'unarchive:trend_follow'])
  expect(archive().textContent).not.toContain('趋势跟随')
  clickTab('策略库')
  expect(libraryOrder()).toEqual(['趋势跟随', '混合叠加'])
  // 恢复只回库, 不自动回池
  expect(choice('趋势跟随').checked).toBe(false)
  expect([...selected().querySelectorAll('[data-strategy-id]')].map(el => el.getAttribute('data-strategy-id')))
    .toEqual(['composite_mix_v1'])
})

it('assigns a group from the row select and filters the library via group chips', async () => {
  const groups = [{ id: 'g1', name: '趋势组' }]
  await render({ groups, ids: ['trend_follow'] })

  setSelect(selectOf('分组：趋势跟随'), 'g1')
  expect(calls).toEqual(['group:trend_follow:g1'])
  expect(library().textContent).toContain('趋势组')

  // 分组 chip 铺开在顶部: 点名称筛选
  act(() => { groupChip('趋势组').click() })
  expect(libraryOrder()).toEqual(['趋势跟随'])
  act(() => { groupChip('未分组').click() })
  expect(libraryOrder()).toEqual(['混合叠加'])
  act(() => { groupChip('全部').click() })
  expect(libraryOrder()).toEqual(['趋势跟随', '混合叠加'])
})

it('group chip checkbox batch-selects the whole group and deselects when all pooled', async () => {
  const groups = [{ id: 'g1', name: '趋势组' }]
  await render({ groups, ids: ['composite_mix_v1'], meta: { trend_follow: mkMeta({ group: 'g1' }) } })

  // 未全在池 → 补齐整组 (trend_follow 入池; composite 不在组内不受影响)
  const box = groupChipBox('趋势组')
  expect(box.checked).toBe(false)
  act(() => { box.click() })
  expect(calls).toContain('reorder:composite_mix_v1,trend_follow')
  expect(onAdded).toHaveBeenCalledWith(['trend_follow'])
  expect(choice('趋势跟随').checked).toBe(true)

  // 组内已全部在池 → 再点整组移出 (composite 保留)
  act(() => { groupChipBox('趋势组').click() })
  expect(calls).toContain('reorder:composite_mix_v1')
  expect(choice('趋势跟随').checked).toBe(false)
  expect(choice('混合叠加').checked).toBe(true)
})

const scopedStrategies = [
  mkStrategy({ id: 'custom_daily', name: '自定义日线趋势', source: 'custom' }),
  mkStrategy({ id: 'custom_minute', name: '自定义分钟趋势', source: 'custom', timeframes: ['1m'] }),
  mkStrategy({ id: 'custom_reversal', name: '自定义日线反转', source: 'custom' }),
  mkStrategy({ id: 'ai_daily', name: 'AI日线趋势', source: 'ai' }),
  mkStrategy({ id: 'ai_minute', name: 'AI分钟趋势', source: 'ai', timeframes: ['1m'] }),
  mkStrategy({ id: 'ai_reversal', name: 'AI日线反转', source: 'ai' }),
  compositePublic,
  aiDraft,
  mkStrategy({ id: 'archived_daily', name: '已归档日线', source: 'custom' }),
]
async function renderScopedPool(ids = ['custom_daily', 'ai_daily', 'composite_mix_v1']) {
  mockStrategyList.mockResolvedValue({ strategies: scopedStrategies })
  await render({
    ids,
    groups: [{ id: 'g1', name: '趋势组' }, { id: 'g2', name: '观察组' }],
    meta: {
      custom_daily: mkMeta({ group: 'g1' }), custom_minute: mkMeta({ group: 'g1' }),
      ai_daily: mkMeta({ group: 'g1' }), ai_minute: mkMeta({ group: 'g2' }),
      composite_mix_v1: mkMeta({ group: 'g2' }),
      ai_breakout_v1: mkMeta({ group: 'g1' }),
      archived_daily: mkMeta({ group: 'g1', archived: true }),
    },
  })
}

it('updates group counts, checked state and empty groups with the source category', async () => {
  await renderScopedPool()
  expect(groupCount('全部')).toBe(7)
  expect(groupCount('趋势组')).toBe(3)
  expect(groupChipBox('趋势组').indeterminate).toBe(true)

  clickAiTab()
  expect(libraryOrder()).toEqual(['AI日线趋势', 'AI分钟趋势', 'AI日线反转'])
  expect(groupCount('全部')).toBe(3)
  expect(groupCount('未分组')).toBe(1)
  expect(groupCount('趋势组')).toBe(1)
  expect(groupCount('观察组')).toBe(1)
  expect(groupChipBox('趋势组').checked).toBe(true)
  expect(groupChipBox('趋势组').indeterminate).toBe(false)
  expect(groupChipBox('全部').indeterminate).toBe(true)

  clickTab('叠加')
  expect(groupCount('全部')).toBe(1)
  expect(groupCount('趋势组')).toBe(0)
  expect(groupChipBox('趋势组').disabled).toBe(true)
  expect(groupChipBox('趋势组').checked).toBe(false)
  expect(groupChipBox('趋势组').indeterminate).toBe(false)
  expect(groupChipBox('全部').checked).toBe(true)
})

it('group selection only toggles the active source and preserves other pooled strategies', async () => {
  await renderScopedPool()
  clickAiTab()
  act(() => { groupChipBox('趋势组').click() })
  expect(calls).toEqual(['reorder:custom_daily,composite_mix_v1'])
  expect(choice('AI日线趋势').checked).toBe(false)
  expect(onAdded).not.toHaveBeenCalled()

  act(() => { groupChipBox('趋势组').click() })
  expect(calls.at(-1)).toBe('reorder:custom_daily,composite_mix_v1,ai_daily')
  expect(onAdded).toHaveBeenCalledWith(['ai_daily'])
})

it('select all adds and removes only selectable strategies in the active source', async () => {
  await renderScopedPool()
  clickAiTab()
  act(() => { groupChipBox('全部').click() })
  expect(calls).toEqual(['reorder:custom_daily,ai_daily,composite_mix_v1,ai_minute,ai_reversal'])
  expect(onAdded).toHaveBeenCalledWith(['ai_minute', 'ai_reversal'])
  expect(groupChipBox('全部').checked).toBe(true)
  // 草稿须先发布, 归档留在档案库, 都不因全选而入池。
  expect(selected().textContent).not.toContain('AI突破')
  expect(selected().textContent).not.toContain('已归档日线')

  act(() => { groupChipBox('全部').click() })
  expect(calls.at(-1)).toBe('reorder:custom_daily,composite_mix_v1')
  expect(groupChipBox('全部').checked).toBe(false)
})

it('uses timeframe and search results for group counts and batch selection', async () => {
  await renderScopedPool(['ai_daily', 'composite_mix_v1', 'custom_minute', 'custom_reversal'])
  clickTab('自定义')
  setSelect(selectOf('策略周期'), '1d')
  expect(groupCount('全部')).toBe(2)
  expect(groupCount('趋势组')).toBe(1)
  expect(groupChipBox('趋势组').checked).toBe(false)
  const input = container.querySelector<HTMLInputElement>('[aria-label="搜索策略"]')!
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => { setter.call(input, '趋势'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  expect(libraryOrder()).toEqual(['自定义日线趋势'])
  expect(groupCount('全部')).toBe(1)
  expect(groupCount('未分组')).toBe(0)
  expect(groupChipBox('未分组').disabled).toBe(true)
  act(() => { groupChipBox('全部').click() })
  expect(calls).toEqual(['reorder:ai_daily,composite_mix_v1,custom_minute,custom_reversal,custom_daily'])
  expect(onAdded).toHaveBeenCalledWith(['custom_daily'])
})

it('keeps group counts independent of the selected group and resets it when switching source', async () => {
  await renderScopedPool()
  act(() => { groupChip('趋势组').click() })
  expect(libraryOrder()).toEqual(['自定义日线趋势', '自定义分钟趋势', 'AI日线趋势'])
  expect(groupCount('观察组')).toBe(2)
  expect(groupCount('全部')).toBe(7)
  clickTab('叠加')
  expect(libraryOrder()).toEqual(['混合叠加'])
})

it('disables group selection when the category only contains unpublished AI drafts', async () => {
  mockStrategyList.mockResolvedValue({ strategies: [aiDraft, builtinResearch] })
  await render({ groups: [{ id: 'g1', name: '趋势组' }] })
  clickAiTab()
  expect(library().textContent).toContain('AI突破')
  expect(library().textContent).toContain('发布')
  expect(groupCount('全部')).toBe(0)
  expect(groupChipBox('全部').disabled).toBe(true)
  expect(groupChipBox('趋势组').disabled).toBe(true)
  expect(calls).toEqual([])
})

it('sorts the library by join time and persists the choice', async () => {
  await render({
    meta: {
      trend_follow: mkMeta({ added_at: '2026-01-01T08:00:00' }),
      composite_mix_v1: mkMeta({ added_at: '2026-02-01T08:00:00' }),
    },
  })
  setSelect(selectOf('策略池排序'), 'added_desc')
  expect(libraryOrder()).toEqual(['混合叠加', '趋势跟随'])
  expect(localStorage.getItem('screener-pool-sort')).toBe(JSON.stringify('added_desc'))

  setSelect(selectOf('策略池排序'), 'added_asc')
  expect(libraryOrder()).toEqual(['趋势跟随', '混合叠加'])
})

it('manages groups in the dedicated modal and deletes a group back to ungrouped', async () => {
  await render({ ids: ['trend_follow'] })
  act(() => { [...container.querySelectorAll('button')].find(b => b.textContent?.includes('管理分组'))!.click() })

  const nameInput = container.querySelector<HTMLInputElement>('[aria-label="新分组名称"]')!
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  act(() => { setter.call(nameInput, '短线'); nameInput.dispatchEvent(new Event('input', { bubbles: true })) })
  await act(async () => { [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === '新建')!.click() })

  expect(calls).toContain('create-group:短线')
  expect(groupChip('短线')).toBeTruthy()

  // 归组后删除分组: 组内策略转未分组, chip 随之消失
  setSelect(selectOf('分组：趋势跟随'), 'g1')
  const delIcon = [...container.querySelectorAll('button')].find(b => b.title.includes('删除分组'))!
  expect(delIcon).toBeTruthy()
  act(() => { delIcon.click() })
  await act(async () => { [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === '确认删除')!.click() })
  expect(calls).toContain('delete-group:g1')
  expect(groupChip('短线')).toBeNull()
})
