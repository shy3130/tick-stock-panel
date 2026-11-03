// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { PluginMetadata } from './PluginMetadata'
import type { PluginDataSourceItem } from '@/lib/api'

let host: HTMLDivElement
let root: Root
const plugin: PluginDataSourceItem = {
  name: 'demo', display_name: '演示数据源', datasets: ['daily'], runtime: 'none',
  available: true, status: '已就绪', description: '', install_hint: '',
}
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})
async function render(fields: Partial<PluginDataSourceItem>) {
  await act(async () => root.render(<PluginMetadata plugin={{ ...plugin, ...fields }} />))
}

it('shows configurable links without requiring API Key configuration', async () => {
  await render({ homepage: 'https://example.com/docs', homepage_label: '使用文档',
    key_manage_url: 'https://example.com/account', key_manage_label: '获取个人 Key' })
  const links = [...host.querySelectorAll('a')]
  expect(links.map(link => link.textContent)).toEqual(['使用文档', '获取个人 Key'])
  expect(links.map(link => link.href)).toEqual(['https://example.com/docs', 'https://example.com/account'])
  expect(links.every(link => link.target === '_blank' && link.rel.includes('noopener'))).toBe(true)
})

it('keeps default labels for old manifests and ignores labels without a URL', async () => {
  await render({ homepage: 'https://example.com', homepage_label: ' ', key_manage_url: 'https://example.com/account' })
  expect([...host.querySelectorAll('a')].map(link => link.textContent)).toEqual(['演示数据源 官网', '管理 Key / 查看账户用量'])
  await render({ homepage_label: '只有名称' })
  expect(host.querySelector('a')).toBeNull()
})

it('renders description and usage notes as Markdown while keeping labels as text', async () => {
  await render({ description: '## 接入说明\n使用 **个人 Key** 和 `不复权` 数据。',
    usage_notes: ['阅读 [接口文档](https://example.com/docs)。'],
    homepage: 'https://example.com', homepage_label: '<b>官网</b>' })
  expect(host.querySelector('strong')?.textContent).toBe('个人 Key')
  expect(host.querySelector('code')?.textContent).toBe('不复权')
  expect(host.querySelector('li a')?.textContent).toBe('接口文档')
  expect(host.querySelector('a b')).toBeNull()
  expect(host.textContent).toContain('<b>官网</b>')
})
