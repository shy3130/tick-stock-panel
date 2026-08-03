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
      freshCount: 18,
      staleCount: 1,
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

function installHealthyFetch(
  overviewPayload: unknown = overview,
  marketPayloads: Record<'cn' | 'hk' | 'us', unknown> = markets,
) {
  const fetchMock = vi.fn((input: string | URL | Request, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/overview')) return jsonResponse(overviewPayload)
    if (url.includes('/markets/cn')) return jsonResponse(marketPayloads.cn)
    if (url.includes('/markets/hk')) return jsonResponse(marketPayloads.hk)
    if (url.includes('/markets/us')) return jsonResponse(marketPayloads.us)
    if (url.includes('/tasks')) return jsonResponse(tasks)
    if (url.includes('/gaps')) return jsonResponse(gaps)
    return jsonResponse({ detail: 'not_found' }, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function installPaginatedFetch() {
  const fetchMock = vi.fn((input: string | URL | Request, _init?: RequestInit) => {
    const rawUrl = String(input)
    const url = new URL(rawUrl, 'http://tickflow.local')
    const offset = Number(url.searchParams.get('offset') ?? 0)
    if (url.pathname.endsWith('/overview')) return jsonResponse(overview)
    if (url.pathname.endsWith('/markets/cn')) return jsonResponse(markets.cn)
    if (url.pathname.endsWith('/markets/hk')) return jsonResponse(markets.hk)
    if (url.pathname.endsWith('/markets/us')) return jsonResponse(markets.us)
    if (url.pathname.endsWith('/tasks')) {
      return jsonResponse({
        ...tasks,
        total: 250,
        limit: 100,
        offset,
        tasks: [{ ...tasks.tasks[0], taskKey: `task-${offset}` }],
      })
    }
    if (url.pathname.endsWith('/gaps')) {
      return jsonResponse({
        ...gaps,
        total: 150,
        limit: 100,
        offset,
        gaps: [{ ...gaps.gaps[0], symbol: `${offset}.HK` }],
      })
    }
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

    expect(screen.getByText('Observation only')).toBeInTheDocument()
    expect(screen.getByText('Live semantic acceptance pending')).toBeInTheDocument()
    expect(screen.queryByText('Live semantic acceptance accepted')).not.toBeInTheDocument()
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

  it('shows totals and navigates task and gap pages with GET-only offsets', async () => {
    const fetchMock = installPaginatedFetch()
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByText('task-0')).toBeInTheDocument()
    const taskPagination = screen.getByRole('group', { name: 'Task pagination' })
    expect(within(taskPagination).getByText('Total 250 · Showing 1–1')).toBeInTheDocument()
    expect(within(taskPagination).getByText('Results are paginated')).toBeInTheDocument()
    expect(within(taskPagination).getByRole('button', { name: 'Previous task page' })).toBeDisabled()

    await user.click(within(taskPagination).getByRole('button', { name: 'Next task page' }))
    expect(await screen.findByText('task-100')).toBeInTheDocument()
    expect(within(taskPagination).getByText('Total 250 · Showing 101–101')).toBeInTheDocument()
    expect(within(taskPagination).getByRole('button', { name: 'Previous task page' })).toBeEnabled()

    const gapPagination = screen.getByRole('group', { name: 'Gap pagination' })
    expect(within(gapPagination).getByText('Total 150 · Showing 1–1')).toBeInTheDocument()
    await user.click(within(gapPagination).getByRole('button', { name: 'Next gap page' }))
    expect(await screen.findByText('100.HK')).toBeInTheDocument()

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([input]) => String(input))
      expect(urls).toContain(
        '/api/collection-monitor/tasks?date=2026-07-26&market=hk&dataset=capital_distribution&limit=100&offset=100',
      )
      expect(urls).toContain(
        '/api/collection-monitor/gaps?market=hk&dataset=capital_distribution&date=2026-07-26&limit=100&offset=100',
      )
    })
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
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

  it('suppresses every conclusion value from a 200 unavailable overview envelope', async () => {
    installHealthyFetch({
      tradeDate: '2026-07-26',
      evidenceState: 'unavailable',
      evidenceAt: null,
      taskCount: 0,
      productionHealthyCount: 0,
      unhealthyTaskCount: 0,
      openGapCount: 0,
      lastConfirmed: { evidenceAt: '2026-07-25T16:00:00+08:00' },
    })

    renderPage()

    const heading = await screen.findByRole('heading', { name: '今日采集结论' })
    const section = heading.closest('section')
    expect(section).not.toBeNull()
    await within(section!).findByText(/最后确认.*2026-07-25T16:00:00\+08:00/)
    for (const label of ['登记任务', '生产健康任务', '异常任务', '开放缺口']) {
      expect(within(section!).getByText(label).parentElement).toHaveTextContent(`${label}—`)
    }
    expect(within(section!).queryByText('0')).not.toBeInTheDocument()
  })

  it('renders current dataset fresh and stale counts with the latest-data timestamp', async () => {
    renderPage()

    const marketMatrix = await screen.findByRole('region', { name: '市场 × 数据集' })
    const hkArticle = within(marketMatrix).getByRole('heading', { name: '港股' }).closest('article')
    expect(hkArticle).not.toBeNull()
    await within(hkArticle!).findByText(/来源 clickhouse.hk_capital_distribution/)
    expect(within(hkArticle!).getByText('新鲜 / 陈旧').nextElementSibling).toHaveTextContent('18 / 1')
    expect(within(hkArticle!).getByText('最新数据').nextElementSibling).toHaveTextContent(
      '2026-07-26T10:29:00+08:00',
    )
  })

  it('labels content update time separately from inspection time', async () => {
    renderPage()

    expect(await screen.findByText('内容更新 2026-07-26T10:29:00+08:00')).toBeInTheDocument()
    expect(screen.getAllByText('巡检 2026-07-26T10:31:30+08:00').length).toBeGreaterThan(0)
  })

  it('renders market temperature evidence without exposing it as a task or gap filter', async () => {
    installHealthyFetch(overview, {
      ...markets,
      hk: {
        ...markets.hk,
        datasets: [
          ...markets.hk.datasets,
          {
            market: 'hk',
            datasetKey: 'market_temperature',
            taskHealth: 'green',
            dataHealth: 'green',
            displayState: 'green',
            status: 'green',
            evidenceState: 'live',
            evidenceAt,
            expectedCount: 1,
            collectedCount: 1,
            missingCount: 0,
            duplicateCount: 0,
            latestDataAt: '2026-07-26T10:31:00+08:00',
            provenance: 'longbridge.market_temperature',
          },
        ],
      },
    })

    renderPage()

    const matrix = await screen.findByRole('region', { name: '市场 × 数据集' })
    const hkArticle = within(matrix).getByRole('heading', { name: '港股' }).closest('article')
    expect(hkArticle).not.toBeNull()
    expect(await within(hkArticle!).findByText('市场温度')).toBeInTheDocument()
    expect(within(hkArticle!).getByText(/来源 longbridge.market_temperature/)).toBeInTheDocument()

    const datasetFilter = screen.getByLabelText('数据集')
    expect(within(datasetFilter).queryByRole('option', { name: '市场温度' })).not.toBeInTheDocument()
    expect(within(datasetFilter).getAllByRole('option')).toHaveLength(5)
  })

  it('renders migrated observation datasets with names and evidence-derived health', async () => {
    installHealthyFetch(overview, {
      ...markets,
      hk: {
        ...markets.hk,
        datasets: [
          { market: 'hk', datasetKey: 'intraday_line', evidenceState: 'live', evidenceAt },
          { market: 'hk', datasetKey: 'order_book_depth', evidenceState: 'live', evidenceAt },
          { market: 'hk', datasetKey: 'realtime_quote', evidenceState: 'live', evidenceAt },
          { market: 'hk', datasetKey: 'trade_tick', evidenceState: 'live', evidenceAt },
          { market: 'hk', datasetKey: 'market_temperature', evidenceState: 'cached', evidenceAt },
        ],
      },
    })

    renderPage()

    const matrix = await screen.findByRole('region', { name: '市场 × 数据集' })
    const hkArticle = within(matrix).getByRole('heading', { name: '港股' }).closest('article')
    expect(hkArticle).not.toBeNull()
    for (const label of ['分时线', '盘口深度', '实时行情', '逐笔成交', '市场温度']) {
      expect(await within(hkArticle!).findByText(label)).toBeInTheDocument()
    }
    expect(within(hkArticle!).getAllByText('健康')).toHaveLength(4)
    expect(within(hkArticle!).getAllByText('降级')).toHaveLength(1)
    expect(within(hkArticle!).queryByText('不可用')).not.toBeInTheDocument()
    expect(within(hkArticle!).queryByText('任务状态')).not.toBeInTheDocument()
  })

  it('shows only bounded last-confirmed detail for an unavailable dataset', async () => {
    installHealthyFetch(overview, {
      ...markets,
      hk: {
        ...markets.hk,
        evidenceState: 'unavailable',
        evidenceAt: null,
        datasets: [{
          market: 'hk',
          datasetKey: 'capital_distribution',
          taskHealth: 'unavailable',
          dataHealth: 'unavailable',
          displayState: 'unavailable',
          status: 'unavailable',
          evidenceState: 'unavailable',
          evidenceAt: null,
          expectedCount: 0,
          collectedCount: 0,
          freshCount: 0,
          staleCount: 0,
          missingCount: 0,
          duplicateCount: 0,
          latestDataAt: '2026-07-26T10:29:00+08:00',
          provenance: 'placeholder.current',
          lastConfirmed: {
            evidenceAt: '2026-07-25T16:00:00+08:00',
            expectedCount: 20,
            collectedCount: 19,
            freshCount: 18,
            staleCount: 1,
            missingCount: 1,
            latestDataAt: '2026-07-25T15:59:00+08:00',
            provenance: 'clickhouse.confirmed',
          },
        }],
      },
    })

    renderPage()

    const matrix = await screen.findByRole('region', { name: '市场 × 数据集' })
    const hkArticle = within(matrix).getByRole('heading', { name: '港股' }).closest('article')
    expect(hkArticle).not.toBeNull()
    await within(hkArticle!).findByText(/来源 clickhouse.confirmed/)
    expect(within(hkArticle!).getByText(
      /预期 20 · 采集 19 · 新鲜 18 · 陈旧 1 · 缺失 1 · 最新数据 2026-07-25T15:59:00\+08:00 · 来源 clickhouse.confirmed/,
    )).toBeInTheDocument()
    for (const currentLabel of ['采集 / 预期', '新鲜 / 陈旧', '缺口 / 重复', '最新数据']) {
      expect(within(hkArticle!).queryByText(currentLabel)).not.toBeInTheDocument()
    }
    expect(within(hkArticle!).queryByText('来源 placeholder.current')).not.toBeInTheDocument()
    expect(within(hkArticle!).queryByText('2026-07-26T10:29:00+08:00')).not.toBeInTheDocument()
  })
})
