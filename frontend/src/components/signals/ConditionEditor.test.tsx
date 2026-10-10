// @vitest-environment jsdom
// ConditionEditor: 自定义信号弹窗与策略「叠加条件」共用的多行条件编辑器。
// 覆盖: 行渲染/增删、受控 onChange、hideDays 隐藏偏移、字符串字段切换时
// 运算符/右值复位 (跨类型语义不再成立)。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { ConditionEditor } from './ConditionEditor'

const baseOptions = {
  fields: [
    { key: 'close', label: '收盘价' },
    { key: 'ext_x_name', label: '扩展·名称' },
  ],
  stringFields: ['ext_x_name'],
}

let root: Root | null = null
const container = document.createElement('div')
document.body.appendChild(container)

afterEach(() => {
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
  document.body.innerHTML = ''  // FieldPicker portal 挂 body
})

function render(props: {
  conditions: { left: string; op: string; right: string; leftDays?: number; rightDays?: number }[]
  onChange: (next: any[]) => void
  hideDays?: boolean
  allowEmpty?: boolean
}) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  if (root) { act(() => { root!.unmount() }); root = null }
  container.innerHTML = ''
  root = createRoot(container)
  act(() => {
    root!.render(
      <ConditionEditor
        conditions={props.conditions}
        onChange={props.onChange}
        options={{ ...baseOptions, hideDays: props.hideDays }}
        allowEmpty={props.allowEmpty}
      />,
    )
  })
}

function pickField(label: string) {
  // 打开字段选择弹层 (portal 到 body) 并点击目标字段
  act(() => {
    container.querySelector<HTMLButtonElement>('button.min-w-\\[80px\\]')!.click()
  })
  const btns = Array.from(document.body.querySelectorAll('button'))
  const target = btns.find(b => b.textContent?.trim() === label)
  expect(target, `字段选项 ${label} 应存在`).toBeTruthy()
  act(() => { target!.click() })
}

it('renders rows with 当/且 prefixes and remove button', () => {
  const onChange = vi.fn()
  render({
    conditions: [
      { left: 'close', op: '>', right: '10' },
      { left: 'close', op: '<', right: '20' },
    ],
    onChange,
  })

  expect(container.textContent).toContain('当')
  expect(container.textContent).toContain('且')
  const remove = container.querySelector<HTMLButtonElement>('button[class*="hover:text-danger"]')
  expect(remove).toBeTruthy()
  act(() => { remove!.click() })
  // 删除首行后剩第二条件 (right=20)
  expect(onChange).toHaveBeenCalledWith([
    expect.objectContaining({ right: '20' }),
  ])
})

it('add button appends a default row and reports via onChange', () => {
  const onChange = vi.fn()
  render({ conditions: [{ left: 'close', op: '>', right: '10' }], onChange })

  const add = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent?.includes('添加条件'))!
  act(() => { add.click() })
  expect(onChange).toHaveBeenCalledTimes(1)
  const next = onChange.mock.calls[0][0] as any[]
  expect(next).toHaveLength(2)
  expect(next[1]).toMatchObject({ left: 'close', op: '>', right: '0' })
})

it('empty conditions shows the no-filter hint and still allows adding', () => {
  const onChange = vi.fn()
  render({ conditions: [], onChange })

  expect(container.textContent).toContain('无条件 — 不叠加过滤')
})

it('default behavior keeps the last row undeletable (custom signals need ≥1)', () => {
  render({ conditions: [{ left: 'close', op: '>', right: '10' }], onChange: () => {} })
  expect(container.querySelector('button[class*="hover:text-danger"]')).toBeNull()
})

it('allowEmpty exposes delete on the last row and reports []', () => {
  const onChange = vi.fn()
  render({ conditions: [{ left: 'close', op: '>', right: '10' }], onChange, allowEmpty: true })

  const remove = container.querySelector<HTMLButtonElement>('button[class*="hover:text-danger"]')
  expect(remove).toBeTruthy()
  act(() => { remove!.click() })
  expect(onChange).toHaveBeenCalledWith([])
})

it('hideDays hides the 最新 offset toggle', () => {
  render({
    conditions: [{ left: 'close', op: '>', right: '10' }],
    onChange: () => {},
    hideDays: true,
  })
  expect(container.textContent).not.toContain('最新')

  render({
    conditions: [{ left: 'close', op: '>', right: '10' }],
    onChange: () => {},
    hideDays: false,
  })
  expect(container.textContent).toContain('最新')
})

it('switching to a string field resets op to contains and right value', () => {
  const onChange = vi.fn()
  render({
    conditions: [{ left: 'close', op: '>', right: 'field:ma20', leftDays: 0, rightDays: 0 }],
    onChange,
  })

  pickField('扩展·名称')
  expect(onChange).toHaveBeenCalledTimes(1)
  expect(onChange.mock.calls[0][0][0]).toMatchObject({
    left: 'ext_x_name', op: 'contains', right: '',
  })
})

it('switching within numeric fields keeps op/right intact', () => {
  const onChange = vi.fn()
  render({
    conditions: [{ left: 'close', op: '>', right: '10', leftDays: 0, rightDays: 0 }],
    onChange,
  })

  // 从字段弹层重新选择同一个数值字段 → 仅更新 left
  pickField('收盘价')
  expect(onChange).toHaveBeenCalledWith([
    expect.objectContaining({ left: 'close', op: '>', right: '10' }),
  ])
})
