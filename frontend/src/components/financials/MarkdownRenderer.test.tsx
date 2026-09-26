// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MarkdownRenderer } from './MarkdownRenderer'

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

async function renderToHtml(content: string): Promise<string> {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()); host.remove() }
  await act(async () => root.render(<MarkdownRenderer content={content} />))
  return host.innerHTML
}

it('colors signed pct and limit words red-up/green-down (A股口径)', async () => {
  const html = await renderToHtml('今日上涨 **+2.35%**，昨日 -1.20%，盘中一度涨停。')
  // 上涨 / +2.35% (含加粗内) / 涨停 → 红; -1.20% → 绿
  expect(html.match(/text-bull/g)?.length).toBe(3)
  expect(html).toContain('text-bear')
  // 加粗内的百分比递归拿到语义色: strong 直接包裹着色 span
  expect(html).toMatch(/<strong[^>]*><span[^>]*text-bull[^>]*>\+2\.35%<\/span><\/strong>/)
  // 涨停词徽章化: 底色 + 着色
  expect(html).toMatch(/bg-bull\/10[^>]*>涨停<\/span>/)
})

it('does not color unsigned pct (direction unknown, e.g. turnover)', async () => {
  const html = await renderToHtml('换手率 3.66%，成交额 1.2亿元。')
  expect(html).not.toContain('text-bull')
  expect(html).not.toContain('text-bear')
  expect(html).toContain('3.66%')
  // 金额数字等宽, 单位留在 span 外
  expect(html).toMatch(/font-mono[^>]*>1\.2<\/span>亿元/)
})

it('colors direction words without background', async () => {
  const html = await renderToHtml('今日上涨，昨日下跌，量能走弱。')
  expect(html.match(/text-bull/g)?.length).toBe(1)
  expect(html.match(/text-bear/g)?.length).toBe(2)
})

it('leaves signed pct inside inline code untouched', async () => {
  const html = await renderToHtml('字段示例 `+1.00%` 为原始文本。')
  expect(html).not.toContain('text-bull')
  expect(html).toContain('<code')
})

it('wide tables (>4 cols) use auto layout, nowrap cells and horizontal scroll', async () => {
  const html = await renderToHtml(
    '| # | 标的 | 收盘 | 当日 | 换手率 | 评分 |\n' +
    '|---|---|---|---|---|---|\n' +
    '| 1 | 金健米业 600127.SH | 7.72 | +1.05% | 3.66% | 82 |',
  )
  // 宽表: 按内容取宽 + 可横向滚动, 单元格不折行(标的/代码不再断行)
  expect(html).toContain('overflow-x-auto')
  expect(html).toContain('table-auto')
  expect(html).toContain('whitespace-nowrap')
  expect(html).not.toContain('table-fixed')
  expect(html).not.toContain('break-words')
})

it('narrow tables (<=4 cols) keep fixed layout with breakable cells', async () => {
  const html = await renderToHtml(
    '| 维度 | 数值 | 判断 |\n' +
    '|---|---|---|\n' +
    '| 营收 | 12.3亿元 | 同比上涨 |\n',
  )
  expect(html).toContain('table-fixed')
  expect(html).toContain('break-words')
  expect(html).not.toContain('overflow-x-auto')
})
