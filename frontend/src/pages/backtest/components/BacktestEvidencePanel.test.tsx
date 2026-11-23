// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { BacktestEvidence } from '@/lib/api'
import { BacktestEvidencePanel } from './BacktestEvidencePanel'

let host: HTMLDivElement
let root: Root
const originalCreateUrl = Object.getOwnPropertyDescriptor(URL, 'createObjectURL')
const originalRevokeUrl = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  for (const [key, descriptor] of [['createObjectURL', originalCreateUrl], ['revokeObjectURL', originalRevokeUrl]] as const) {
    if (descriptor) Object.defineProperty(URL, key, descriptor)
    else Reflect.deleteProperty(URL, key)
  }
})

function evidence(status: BacktestEvidence['lookahead']['status'] = 'passed'): BacktestEvidence {
  return {
    schema_version: 1, run_id: '012345abcd', created_at: '2026-11-01T02:00:00Z',
    runtime: { application: '0.3.25', python: '3.11', polars: '1', numpy: '2', code_sha256: 'e'.repeat(64) },
    config: {}, config_sha256: 'c'.repeat(64),
    strategy: { id: 'test', backend: 'matrix_native', sha256: 'b'.repeat(64), changed_during_run: false },
    data: { sha256: 'a'.repeat(64), asset_type: 'stock', origin: 'local_normalized_snapshot', daily_price_basis: 'qfq', datasets: {} },
    reproducibility_sha256: 'd'.repeat(64),
    lookahead: { status, sampled: true, differences: 0, checks: [], reports: [] },
    persistence: { status: 'saved' }, limitations: [],
  }
}

it('历史结果缺少记录时明确提示重新回测，不展示检查通过', async () => {
  await act(async () => root.render(<BacktestEvidencePanel />))
  expect(host.textContent).toContain('重新回测后自动生成')
  expect(host.textContent).not.toContain('抽样未发现差异')
})

it('通过状态注明抽样范围，展示独立版本并导出完整核查报告', async () => {
  const value = evidence()
  const createUrl = vi.fn((_blob: Blob) => 'blob:test')
  const revoke = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', { value: createUrl, configurable: true })
  Object.defineProperty(URL, 'revokeObjectURL', { value: revoke, configurable: true })
  let filename = ''
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { filename = this.download })
  await act(async () => root.render(<BacktestEvidencePanel evidence={value} />))
  expect(host.textContent).toContain('抽样未发现差异')
  expect(host.textContent).not.toContain('保证没有未来数据')
  expect(host.textContent).toContain('数据版本')
  expect(host.textContent).toContain('a'.repeat(20))
  expect(host.textContent).toContain('参数版本')
  expect(host.querySelector('details')?.open).toBe(false)
  await act(async () => host.querySelector('button')!.click())
  expect(filename).toBe('backtest-evidence-012345abcd.json')
  expect(createUrl).toHaveBeenCalledOnce()
  expect(createUrl.mock.calls[0][0]).toBeInstanceOf(Blob)
  expect(revoke).toHaveBeenCalledWith('blob:test')
})

it('发现差异时自动展开具体时间、标的、字段与重算值', async () => {
  const value = evidence('failed')
  value.lookahead.differences = 1
  value.lookahead.checks = [{
    kind: 'truncate', cutoff: '2024-01-05', status: 'completed', compared_values: 10, differences: 1,
    examples: [{ date: '2024-01-05', symbol: '600000.SH', field: 'score', cutoff: '2024-01-05', baseline: 30, recomputed: 10 }],
  }]
  await act(async () => root.render(<BacktestEvidencePanel evidence={value} />))
  expect(host.querySelector('details')?.open).toBe(true)
  expect(host.textContent).toContain('检测到历史输出变化')
  expect(host.textContent).toContain('600000.SH')
  expect(host.textContent).toContain('评分')
  expect(host.textContent).toContain('30 → 10')
})

it('检查错误与保存失败保持可见，并允许导出当前内存中的报告', async () => {
  const value = evidence('error')
  value.execution_timing = { status: 'failed', compared_values: 1, differences: 1, examples: [] }
  value.persistence = { status: 'error' }
  value.lookahead.checks = [{ kind: 'truncate', cutoff: '2024-01-05', status: 'error', differences: 0, compared_values: 0, examples: [], error: '重算未完成（ValueError）' }]
  await act(async () => root.render(<BacktestEvidencePanel evidence={value} />))
  expect(host.textContent).toContain('检查未完成')
  expect(host.textContent).toContain('版本记录未能保存')
  expect(host.textContent).toContain('信号与成交时间关系异常')
  expect(host.textContent).toContain('重算未完成（ValueError）')
  expect(host.querySelector('button')?.disabled).toBe(false)
})
