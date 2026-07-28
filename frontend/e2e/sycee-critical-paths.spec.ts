import { expect, test, type Browser, type Page, type Route } from '@playwright/test'

const ADMIN_PASSWORD = 'e2e-admin-pass'
const MEMBER_PASSWORD = 'e2e-member-pass'

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

async function mockUpstreamApis(page: Page) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (
      path.startsWith('/api/auth/')
      || path.startsWith('/api/invite/')
      || path.startsWith('/api/sycee/')
      || path.startsWith('/api/public/sycee/')
      || path === '/api/settings/onboarding/complete'
    ) {
      await route.continue()
      return
    }

    if (path === '/api/settings') {
      await json(route, {
        mode: 'none',
        tickflow_api_key_masked: '',
        has_tickflow_key: false,
        tier_label: 'None',
        current_endpoint: '',
        probe_log: [],
        missing_caps: [],
        extras_caps: [],
        onboarding_completed: true,
        ai_provider: 'openai_compat',
        ai_base_url: '',
        ai_api_key_masked: '',
        has_ai_key: false,
        ai_configured: false,
        ai_model: '',
        ai_user_agent: '',
      })
      return
    }
    if (path === '/api/settings/preferences') {
      await json(route, {
        realtime_quotes_enabled: false,
        indices_nav_pinned: false,
        nav_order: [],
        nav_hidden: [],
        sse_refresh_pages: {},
      })
      return
    }
    if (path === '/api/capabilities') {
      await json(route, { label: 'None', capabilities: {} })
      return
    }
    if (path === '/api/data/version') {
      await json(route, { version: '1.1.0' })
      return
    }
    if (path === '/api/intraday/status') {
      await json(route, {
        enabled: false,
        running: false,
        interval_s: 60,
        symbol_count: 0,
        quote_age_ms: null,
        is_trading_hours: false,
        last_fetch_ms: null,
      })
      return
    }
    if (path === '/api/pipeline/jobs') {
      await json(route, { active_id: null, jobs: [] })
      return
    }
    if (path === '/api/alerts') {
      await json(route, { alerts: [], total: 0 })
      return
    }
    if (path === '/api/analysis-menus') {
      await json(route, { items: [] })
      return
    }
    if (path === '/api/intraday/indices') {
      await json(route, { rows: [], count: 0 })
      return
    }
    if (path === '/api/strategies') {
      await json(route, {
        strategies: [{
          id: 'builtin-e2e-exit',
          name: 'E2E 卖出策略',
          description: 'Deterministic browser-test fixture',
          exit_signals: ['exit_signal'],
        }],
      })
      return
    }
    if (path === '/api/monitor-rules') {
      await json(route, { rules: [] })
      return
    }
    if (path === '/api/kline/instruments/search') {
      const q = (url.searchParams.get('q') ?? '').toUpperCase()
      const fixtures = [
        { symbol: '600519.SH', code: '600519', name: '贵州茅台', asset_type: 'stock' },
        { symbol: '000001.SZ', code: '000001', name: '平安银行', asset_type: 'stock' },
      ]
      await json(route, { results: fixtures.filter(item => item.symbol.includes(q) || item.name.includes(q)) })
      return
    }
    if (path === '/api/kline/daily') {
      const symbol = url.searchParams.get('symbol') ?? ''
      await json(route, {
        symbol,
        name: symbol === '600519.SH' ? '贵州茅台' : '平安银行',
        rows: [{ date: '2026-07-27', close: symbol === '600519.SH' ? 1420 : 11.5, change_pct: 0.8 }],
      })
      return
    }

    await json(route, {})
  })
}

async function completeOnboarding(page: Page) {
  const response = await page.request.post('/api/settings/onboarding/complete')
  expect(response.ok()).toBeTruthy()
}

async function loginAdmin(page: Page) {
  await mockUpstreamApis(page)
  await page.goto('/login')
  await page.getByPlaceholder('用户名').fill('admin')
  await page.getByPlaceholder('访问密码').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForURL(url => url.pathname !== '/login')
  await completeOnboarding(page)
}

async function createTrade(page: Page, input: {
  symbol: string
  name: string
  side?: '买入' | '卖出'
  quantity: string
  price: string
}) {
  await page.getByRole('button', { name: '记录交易' }).click()
  const dialog = page.getByRole('dialog', { name: '记录一笔交易' })
  if (input.side === '卖出') await dialog.getByRole('button', { name: '卖出' }).click()
  await dialog.getByLabel(/股票代码/).fill(input.symbol)
  await dialog.getByLabel('股票名称').fill(input.name)
  await dialog.getByLabel(/数量/).fill(input.quantity)
  await dialog.getByLabel(/成交价/).fill(input.price)
  await dialog.getByRole('button', { name: '保存交易' }).click()
  return dialog
}

async function assertAdminPortfolio(browser: Browser) {
  const context = await browser.newContext()
  const page = await context.newPage()
  await loginAdmin(page)
  const response = await page.request.get('/api/sycee/portfolio')
  expect(response.ok()).toBeTruthy()
  const portfolio = await response.json()
  expect(portfolio.trades).toHaveLength(1)
  expect(portfolio.positions).toEqual(expect.arrayContaining([
    expect.objectContaining({ symbol: '600519.SH', quantity: 100 }),
  ]))
  await context.close()
}

test.describe.serial('Sycee v1.1 critical data journeys', () => {
  test('bootstraps the administrator through the real authentication UI', async ({ page }) => {
    await mockUpstreamApis(page)
    await page.goto('/login')
    await expect(page.getByText('设置管理员账户')).toBeVisible()
    await page.getByPlaceholder('访问密码').fill(ADMIN_PASSWORD)
    await page.getByPlaceholder('再次输入密码').fill(ADMIN_PASSWORD)
    await page.getByRole('button', { name: '设置并进入' }).click()
    await page.waitForURL(url => url.pathname !== '/login')
    await completeOnboarding(page)
    await page.goto('/portfolio')
    await expect(page.getByRole('heading', { name: '我的持仓' })).toBeVisible()
  })

  test('persists a trade and rejects a historical oversell', async ({ page }) => {
    await loginAdmin(page)
    await page.goto('/portfolio')
    await expect(page.getByRole('heading', { name: '我的持仓' })).toBeVisible()

    await createTrade(page, {
      symbol: '600519.SH',
      name: '贵州茅台',
      quantity: '100',
      price: '1400',
    })
    await expect(page.getByText('交易记录已保存')).toBeVisible()
    await expect(page.getByText('600519.SH').first()).toBeVisible()

    const oversell = await createTrade(page, {
      symbol: '600519.SH',
      name: '贵州茅台',
      side: '卖出',
      quantity: '101',
      price: '1450',
    })
    await expect(oversell.getByRole('alert')).toContainText('可卖数量为 100')
    await oversell.getByRole('button', { name: '取消' }).click()

    await page.reload()
    await expect(page.getByText('600519.SH').first()).toBeVisible()
    const response = await page.request.get('/api/sycee/portfolio')
    expect((await response.json()).trades).toHaveLength(1)
  })

  test('registers an invited member and keeps both portfolios isolated', async ({ browser }) => {
    const memberContext = await browser.newContext()
    const memberPage = await memberContext.newPage()
    await mockUpstreamApis(memberPage)
    await memberPage.goto('/invite?redirect=%2Fportfolio')
    await expect(memberPage.getByRole('heading', { name: '内测访问' })).toBeVisible()
    await memberPage.getByLabel('邀请码').fill('E2E-ALICE')
    await memberPage.getByLabel('用户名').fill('alice')
    await memberPage.getByLabel('登录密码').fill(MEMBER_PASSWORD)
    await memberPage.getByLabel('确认密码').fill(MEMBER_PASSWORD)
    await memberPage.getByRole('button', { name: '创建并进入' }).click()
    await memberPage.waitForURL(url => url.pathname !== '/invite')
    await completeOnboarding(memberPage)
    await memberPage.goto('/portfolio')

    const empty = await memberPage.request.get('/api/sycee/portfolio')
    expect((await empty.json()).trades).toHaveLength(0)
    await createTrade(memberPage, {
      symbol: '000001.SZ',
      name: '平安银行',
      quantity: '200',
      price: '11',
    })
    await expect(memberPage.getByText('000001.SZ').first()).toBeVisible()
    await memberContext.close()

    await assertAdminPortfolio(browser)
  })

  test('publishes and revokes a read-only research snapshot', async ({ browser, page }) => {
    await loginAdmin(page)
    await page.goto('/research-ledger')
    await page.getByRole('button', { name: '新建研究' }).first().click()
    const editor = page.getByRole('dialog', { name: '建立研究记录' })
    await editor.getByLabel(/标题/).fill('E2E 渠道库存验证')
    await editor.getByRole('textbox', { name: '对象', exact: true }).fill('600519.SH')
    await editor.getByLabel('核心判断').fill('渠道库存改善将先反映在批价稳定性。')
    await editor.getByLabel('支持证据').fill('批价连续三周企稳')
    await editor.getByLabel('反方证据').fill('终端动销仍可能偏弱')
    await editor.getByLabel('失效条件').fill('批价重新跌破观察区间')
    await editor.getByLabel('下一步').fill('下周复核渠道库存')
    await editor.getByRole('button', { name: '保存记录' }).click()
    await expect(
      page.getByLabel('研究记录详情').getByRole('heading', { name: 'E2E 渠道库存验证' }),
    ).toBeVisible()

    await page.getByRole('button', { name: '分享' }).click()
    const shareDialog = page.getByRole('dialog', { name: '只读分享' })
    await shareDialog.getByRole('button', { name: '创建分享' }).click()
    const publicUrl = await shareDialog.getByRole('link', { name: '打开' }).getAttribute('href')
    expect(publicUrl).toMatch(/\/share\/research\//)

    const publicContext = await browser.newContext()
    const publicPage = await publicContext.newPage()
    await publicPage.goto(publicUrl!)
    await expect(publicPage.getByRole('heading', { name: 'E2E 渠道库存验证' })).toBeVisible()
    await expect(publicPage.getByText('渠道库存改善将先反映在批价稳定性。')).toBeVisible()

    await shareDialog.getByRole('button', { name: '撤销' }).click()
    await page.getByRole('button', { name: '撤销分享' }).click()
    await expect(page.getByText('只读分享已撤销')).toBeVisible()
    await publicPage.reload()
    await expect(publicPage.getByText('分享不存在或已撤销')).toBeVisible()
    await publicContext.close()
  })

  test('restores deleted user data from a downloaded snapshot', async ({ page }) => {
    await loginAdmin(page)
    await page.goto('/data-backup')
    await expect(page.getByRole('heading', { name: '数据备份' })).toBeVisible()
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出快照' }).click()
    const download = await downloadPromise
    const backupPath = await download.path()
    expect(backupPath).not.toBeNull()

    const portfolioResponse = await page.request.get('/api/sycee/portfolio')
    const portfolio = await portfolioResponse.json()
    const remove = await page.request.delete(`/api/sycee/portfolio/trades/${portfolio.trades[0].id}`)
    expect(remove.ok()).toBeTruthy()
    expect((await (await page.request.get('/api/sycee/portfolio')).json()).trades).toHaveLength(0)

    await page.getByRole('button', { name: '选择文件' }).click()
    await page.locator('input[type="file"]').setInputFiles(backupPath!)
    await expect(page.getByText(/1 笔交易/)).toBeVisible()
    await page.getByRole('button', { name: '恢复数据' }).click()
    await page.getByRole('button', { name: '恢复数据' }).last().click()
    await expect(page.getByText('Sycee 数据已恢复，原数据已保存为安全副本')).toBeVisible()

    const restored = await page.request.get('/api/sycee/portfolio')
    expect((await restored.json()).trades).toHaveLength(1)
  })

  test('keeps the portfolio workflow usable on a phone viewport', async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
    const page = await context.newPage()
    await loginAdmin(page)
    await page.goto('/portfolio')
    await expect(page.getByRole('heading', { name: '我的持仓' })).toBeVisible()
    await expect(page.getByRole('button', { name: '记录交易' })).toBeVisible()
    const mobilePosition = page.getByRole('region', { name: '当前持仓' })
      .locator('article')
      .filter({ hasText: '600519.SH' })
    await expect(mobilePosition).toBeVisible()
    await context.close()
  })
})
