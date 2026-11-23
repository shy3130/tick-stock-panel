// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { buildExtColumnsParam, serializeColumns, type ColumnConfig } from '@/lib/list-columns'
import { ListColumnCustomizerInner } from './ListColumnCustomizerInner'

let root: Root | undefined
let host: HTMLDivElement
let client: QueryClient
let columns: ColumnConfig[]

const column = (fieldName = 'net'): ColumnConfig => ({
  id: `ext:flow:${fieldName}`, label: fieldName, visible: true, align: 'center',
  source: { type: 'ext', configId: 'flow', fieldName, fieldType: 'DOUBLE' },
  extDisplay: { displayMode: 'text', snapshotRender: 'bar', snapshotAt: '09:20:00', chartDays: 5 },
})

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  host = document.createElement('div')
  document.body.appendChild(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(async () => {
  if (root) await act(async () => root!.unmount())
  root = undefined
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function render({ granularity = 'intraday', timeField = 'ts', mode = 'timeseries', initial = [column()] }: {
  granularity?: 'daily' | 'intraday'; timeField?: string | null; mode?: string; initial?: ColumnConfig[]
} = {}) {
  vi.spyOn(api, 'extDataSchemaAll').mockResolvedValue({ items: [{
    id: 'flow', label: '资金流', mode, time_field: timeField, granularity,
    columns: [{ name: 'net', label: 'net', type: 'DOUBLE' }],
  }] })
  client.setQueryData(QK.extDataSchemaAll, { items: [{
    id: 'flow', label: '资金流', mode, time_field: timeField, granularity,
    columns: [{ name: 'net', label: 'net', type: 'DOUBLE' }],
  }] })
  function Harness() {
    const [value, setValue] = useState(initial)
    columns = value
    return <ListColumnCustomizerInner open columns={value} groups={[]} onChange={setValue} onClose={() => {}} />
  }
  root = createRoot(host)
  await act(async () => {
    root!.render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  })
  await act(async () => {
    host.querySelector<HTMLButtonElement>('[data-col-row] button[title="点击配置"]')!.click()
  })
}

async function select(label: string, value: string) {
  const input = host.querySelector<HTMLSelectElement>(`select[aria-label="${label}"]`)
  expect(input, `应能配置${label}`).not.toBeNull()
  await act(async () => {
    input!.value = value
    input!.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

it('多盘列可按日展示并保存, 切回日内保留原取盘时刻与图形设置', async () => {
  await render()
  await select('显示周期', 'daily')
  expect(host.textContent).toContain('天数')
  expect(host.textContent).not.toContain('取盘时刻')
  expect(serializeColumns(columns)[0].extDisplay).toMatchObject({ seriesGranularity: 'daily', chartDays: 5 })
  expect(buildExtColumnsParam(columns)).toBe('flow.net')
  await select('显示周期', 'intraday')
  const kind = [...host.querySelectorAll('select')].find(el => [...el.options].some(o => o.value === 'bar'))!
  await act(async () => { kind.value = 'text'; kind.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.textContent).toContain('取盘时刻')
  expect(columns[0].extDisplay).toMatchObject({ snapshotAt: '09:20:00', chartDays: 5 })
  expect(buildExtColumnsParam(columns)).toBe('flow.net@09:20:00')
})

it('整表默认按日, 有时间列时仍可让单列按日内展示', async () => {
  await render({ granularity: 'daily' })
  await select('显示周期', 'intraday')
  expect(host.textContent).not.toContain('天数')
  expect(columns[0].extDisplay?.seriesGranularity).toBe('intraday')
  await select('显示周期', 'auto')
  expect(host.textContent).toContain('天数')
  expect(columns[0].extDisplay?.seriesGranularity).toBeUndefined()
  expect(buildExtColumnsParam(columns, [{ id: 'flow', granularity: 'daily' }])).toBe('flow.net')
  expect(buildExtColumnsParam(columns, [{ id: 'flow', granularity: 'intraday' }])).toBe('flow.net@09:20:00')
})

it('每列单独选择周期, 不改同表的其他列或拉取配置', async () => {
  const savePull = vi.spyOn(api, 'extDataPullConfig')
  await render({ initial: [column(), column('ratio')] })
  await select('显示周期', 'daily')
  expect(columns[1].extDisplay?.seriesGranularity).toBeUndefined()
  expect(savePull).not.toHaveBeenCalled()
})

it('没有时间列时禁用日内选项, 按日仍然可选', async () => {
  await render({ granularity: 'daily', timeField: null })
  const input = host.querySelector<HTMLSelectElement>('select[aria-label="显示周期"]')
  expect(input).not.toBeNull()
  expect(input!.querySelector<HTMLOptionElement>('option[value="intraday"]')!.disabled).toBe(true)
  expect(input!.querySelector<HTMLOptionElement>('option[value="daily"]')!.disabled).toBe(false)
})

it('快照表不提供不存在的历史序列选项', async () => {
  await render({ mode: 'snapshot', timeField: null })
  expect(host.querySelector('select[aria-label="显示周期"]')).toBeNull()
})
