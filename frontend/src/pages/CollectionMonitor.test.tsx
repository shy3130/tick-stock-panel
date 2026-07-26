import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CollectionMonitor } from './CollectionMonitor'

const evidenceAt = '2026-07-26T10:31:30+08:00'

const overview = {
  tradeDate: '2026-07-26',
  evidenceState: 'live',
  evidenceAt,
  taskCount: 4,
  productionHealthyCount: 3,
  unhealthyTaskCount: 1,
  openGapCount: 2,
}

const markets = {
  cn: {
    market: 'cn',
    tradeDate: '2026-07-26',
    evidenceState: 'live',
    evidenceAt,
    datasets: [{
      market: 'cn',
      datasetKey: 'capital_distribution',
      taskHealth: 'green',
      dataHealth: 'green',
      displayState: 'green',
      status: 'green',
      evidenceState: 'live',
      evidenceAt,
      expectedCount: 10,
      collectedCount: 10,
      missingCount: 0,
      duplicateCount: 0,
      latestDataAt: '2026-07-26T10:30:00+08:00',
      provenance: 'clickhouse.cn_capital_distribution',
    }],
  },
  hk: {
    market: 'hk',
    tradeDate: '2026-07-26',
    evidenceState: 'cached',
    evidenceAt,
    datasets: [{
      market: 'hk',
      datasetKey: 'capital_distribution',
      taskHealth: 'green',
      dataHealth: 'yellow',
      displayState: 'yellow',
      status: 'yellow',
      evidenceState: 'cached',
      evidenceAt,
      expectedCount: 20,
      collectedCount: 19,
      missingCount: 1,
      duplicateCount: 0,
      latestDataAt: '2026-07-26T10:29:00+08:00',
      provenance: 'clickhouse.hk_capital_distribution',
    }],
  },
  us: {
    market: 'us',
    tradeDate: '2026-07-26',
    evidenceState: 'unavailable',
    evidenceAt: null,
    lastConfirmed: {
      evidenceAt: '2026-07-25T16:00:00-04:00',
      expectedCount: 8,
      collectedCount: 8,
      freshCount: 8,
      staleCount: 0,
      missingCount: 0,
      latestDataAt: '2026-07-25T15:59:00-04:00',
      provenance: 'clickhouse.us_capital_distribution',
    },
    datasets: [],
  },
}

const tasks = {
  tradeDate: '2026-07-26',
  evidenceState: 'live',
  evidenceAt,
  total: 2,
  limit: 100,
  offset: 0,
  tasks: [
    {
      taskKey: 'capital-hk',
      technology: 'rust',
      mode: 'production',
      status: 'green',
      markets: ['hk'],
      datasets: ['capital_distribution'],
      heartbeatAt: evidenceAt,
      lastSuccessAt: evidenceAt,
      lastWriteAt: '2026-07-26T10:30:00+08:00',
      rowsWritten: 19,
      rowsFailed: 0,
      retryCount: 0,
      queueDepth: 0,
      evidenceState: 'live',
      evidenceAt,
      provenance: 'collector.capital-hk',
    },
    {
      taskKey: 'capital-shadow',
      technology: 'python',
      mode: 'shadow',
      status: 'yellow',
      markets: ['hk'],
      datasets: ['capital_distribution'],
      heartbeatAt: evidenceAt,
      lastSuccessAt: evidenceAt,
      lastWriteAt: '2026-07-26T10:29:00+08:00',
      rowsWritten: 18,
      rowsFailed: 1,
      retryCount: 1,
      queueDepth: 1,
      evidenceState: 'cached',
      evidenceAt,
      provenance: 'collector.capital-shadow',
    },
  ],
}

const gaps = {
  tradeDate: '2026-07-26',
  market: 'hk',
  datasetKey: 'capital_distribution',
  evidenceState: 'cached',
  evidenceAt,
  total: 1,
  limit: 100,
  offset: 0,
  gaps: [{
    market: 'hk',
    datasetKey: 'capital_distribution',
    symbol: '0700.HK',
    startMinute: '2026-07-26T10:28:00+08:00',
    endMinute: '2026-07-26T10:29:00+08:00',
    expectedCount: 2,
    missingCount: 2,
    gapState: 'open',
    provenance: 'clickhouse.minute_integrity',
    evidenceState: 'cached',
    evidenceAt,
  }],
}

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response)
}

function installHealthyFetch() {
  const fetchMock = vi.fn((input: string | URL | Request, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/overview')) return jsonResponse(overview)
    if (url.includes('/markets/cn')) return jsonResponse(markets.cn)
    if (url.includes('/markets/hk')) return jsonResponse(markets.hk)
    if (url.includes('/markets/us')) return jsonResponse(markets.us)
    if (url.includes('/tasks')) return jsonResponse(tasks)
    if (url.includes('/gaps')) return jsonResponse(gaps)
    return jsonResponse({ detail: 'not_found' }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <CollectionMonitor initialDate="2026-07-26" />
    </QueryClientProvider>,
  )
}

describe('CollectionMonitor', () => {
  beforeEach(() => {
    installHealthyFetch()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the four read-only evidence levels with distinct evidence semantics', async () => {
    renderPage()

    expect(await screen.findByRole('heading', { name: '今日采集结论' })).toBeInTheDocument()
    expect(await screen.findByText('4')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()

    const matrix = screen.getByRole('region', { name: '市场 × 数据集' })
    expect(within(matrix).getByRole('heading', { name: 'A股' })).toBeInTheDocument()
    expect(within(matrix).getByRole('heading', { name: '港股' })).toBeInTheDocument()
    expect(within(matrix).getByRole('heading', { name: '美股' })).toBeInTheDocument()
    expect(within(matrix).getAllByText('实时证据').length).toBeGreaterThan(0)
    expect(within(matrix).getAllByText('陈旧 / 缓存证据').length).toBeGreaterThan(0)
    expect(within(matrix).getAllByText('证据不可用').length).toBeGreaterThan(0)
    expect(within(matrix).getByText(/最后确认.*2026-07-25T16:00:00-04:00/)).toBeInTheDocument()
    expect(within(matrix).getByText(
      /预期 8 · 采集 8 · 新鲜 8 · 陈旧 0 · 缺失 0 · 最新数据 2026-07-25T15:59:00-04:00 · 来源 clickhouse.us_capital_distribution/,
    )).toBeInTheDocument()

    const taskTable = screen.getByRole('table', { name: '采集任务' })
    expect(within(taskTable).getByText('capital-hk')).toBeInTheDocument()
    expect(within(taskTable).getByText('生产')).toBeInTheDocument()
    expect(within(taskTable).getByText('影子观察')).toBeInTheDocument()
    expect(within(taskTable).getByText('collector.capital-hk')).toBeInTheDocument()

    const gapTable = screen.getByRole('table', { name: '缺口证据' })
    expect(within(gapTable).getByText('0700.HK')).toBeInTheDocument()
    expect(within(gapTable).getByText('clickhouse.minute_integrity')).toBeInTheDocument()

    for (const controlName of ['重启', '修复', '确认', '排期', '操作']) {
      expect(screen.queryByRole('button', { name: new RegExp(controlName) })).not.toBeInTheDocument()
    }
  })

  it('uses only the fixed same-origin GET query contract and applies filters', async () => {
    const fetchMock = installHealthyFetch()
    const user = userEvent.setup()
    renderPage()

    await screen.findByText('capital-hk')
    await user.selectOptions(screen.getByLabelText('状态'), 'yellow')
    await user.selectOptions(screen.getByLabelText('技术'), 'python')
    await user.selectOptions(screen.getByLabelText('模式'), 'shadow')

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([input]) => String(input))
      expect(urls).toContain(
        '/api/collection-monitor/tasks?date=2026-07-26&status=yellow&technology=python&market=hk&dataset=capital_distribution&mode=shadow&limit=100&offset=0',
      )
    })

    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(expect.arrayContaining([
      '/api/collection-monitor/overview?date=2026-07-26',
      '/api/collection-monitor/markets/cn?date=2026-07-26',
      '/api/collection-monitor/markets/hk?date=2026-07-26',
      '/api/collection-monitor/markets/us?date=2026-07-26',
      '/api/collection-monitor/gaps?market=hk&dataset=capital_distribution&date=2026-07-26&limit=100&offset=0',
    ]))
  })

  it('fails closed on a 503 without presenting healthy fallback evidence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse(
      { detail: 'collection_monitoring_evidence_unavailable' },
      503,
    )))

    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent('采集证据当前不可用')
    expect(screen.queryByText('capital-hk')).not.toBeInTheDocument()
    expect(screen.queryByText('0700.HK')).not.toBeInTheDocument()
    expect(screen.queryByText('实时证据')).not.toBeInTheDocument()
  })
})
