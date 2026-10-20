// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it } from 'vitest'
import { MiniIntraday } from './MiniIntraday'
import { AUCTION_WIDTH_RATIOS, type AuctionBarWidth } from '@/lib/list-columns'
import type { MinuteKlineRow } from '@/lib/api'

const BULL = '#C74040'
const BEAR = '#2D9B65'
const GOLD = '#E0B84A'
const BG = '#7A7A85'

function mkRows(closes: number[], open?: number): MinuteKlineRow[] {
  return closes.map((c, i) => ({
    datetime: `2026-10-19 09:${String(30 + i).padStart(2, '0')}:00`,
    open: i === 0 ? (open ?? c) : c, high: c, low: c, close: c, volume: 100, amount: 1000,
  }))
}

let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function render(props: Parameters<typeof MiniIntraday>[0]) {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  act(() => { root.render(<MiniIntraday {...props} />) })
  return host
}

/** 最后一条 polyline 是连续竞价价格线; 竞价线的 stroke 用同色但 opacity .9 */
function polylines(host: HTMLElement) {
  return Array.from(host.querySelectorAll('polyline'))
}

/** 评审8: rect 断言先按 fill 过滤 (金 #E0B84A / 红绿 {#C74040,#2D9B65}),
 * 排除窄栏底色 #7A7A85, 再计数/求交 */
function barsByFill(host: HTMLElement, fills: string[]) {
  return Array.from(host.querySelectorAll('rect')).filter(r => fills.includes(r.getAttribute('fill') ?? ''))
}

const rectCenterX = (r: Element) => Number(r.getAttribute('x')) + Number(r.getAttribute('width')) / 2
const rectTopY = (r: Element) => Number(r.getAttribute('y'))
const rectBottomY = (r: Element) => Number(r.getAttribute('y')) + Number(r.getAttribute('height'))

/** polyline 的 x 经 toFixed(1) 量化, 与桶中心 (未量化 float) 比较容差取半步 0.05+ε */
const expectNear = (actual: number, expected: number) => {
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(0.06)
}

/** 折线 x 坐标序列 */
function lineXs(line: Element): number[] {
  return (line.getAttribute('points') ?? '').trim().split(/\s+/).map(p => Number(p.split(',')[0]))
}

function lineYs(line: Element): number[] {
  return (line.getAttribute('points') ?? '').trim().split(/\s+/).map(p => Number(p.split(',')[1]))
}

/** 主体量带几何 (与组件同式): volActive 时价格区止于 ~64%, 量带独立于 [volTop, H-3] */
function bandGeom(H: number) {
  const padY = 3
  const volTop = Math.round(H * 0.64) + 2
  return { volBandH: (H - padY) - volTop, bandTop: volTop, bottomY: H - padY }
}

/** 竞价栏量柱: fill 过滤后再按 x 落在窄栏内筛 (右侧分钟量柱同为红/绿, 必须排除);
 * W → auctionW 与组件同式 (paneGap=4), 比例取共享真值表 (不镜像公式) */
function auctionBarsByFill(host: HTMLElement, fills: string[], W: number, width: AuctionBarWidth = 'standard') {
  const auctionW = Math.max(24, Math.round((W - 4) * AUCTION_WIDTH_RATIOS[width]))
  return barsByFill(host, fills).filter(r => rectCenterX(r) <= auctionW)
}

it('配置竞价后渲染左栏: 多一条竞价价格线 + 分隔线', () => {
  const host = render({
    rows: mkRows([10.1, 10.2, 10.3], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auction: { price: [['09:15:00', 9.9], ['09:20:00', 10.0], ['09:25:00', 10.0]] },
  })
  // 无竞价时 2 条线 (均价 + 价格); 有竞价时 +1
  expect(polylines(host)).toHaveLength(3)
})

it('无竞价配置时保持历史渲染 (2 条折线, 铺满整幅)', () => {
  const host = render({ rows: mkRows([10.1, 10.2, 10.3], 10.0), prevClose: 10, width: 150, height: 80 })
  expect(polylines(host)).toHaveLength(2)
  const price = polylines(host)[1].getAttribute('points') ?? ''
  const xs = price.trim().split(/\s+/).map(p => Number(p.split(',')[0]))
  expect(xs[0]).toBe(0)
  expect(xs[xs.length - 1]).toBe(150)
})

it('有竞价时连续竞价让出左栏: 起点右移', () => {
  const host = render({
    rows: mkRows([10.1, 10.2, 10.3], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auction: { price: [['09:15:00', 9.9], ['09:25:00', 10.0]] },
  })
  const price = polylines(host)[polylines(host).length - 1].getAttribute('points') ?? ''
  const xs = price.trim().split(/\s+/).map(p => Number(p.split(',')[0]))
  expect(xs[0]).toBeGreaterThan(30)
  expect(xs[xs.length - 1]).toBeCloseTo(150, 0)
})

it('竞价价格参与共用纵轴: 高开竞价不跑出画面', () => {
  // 分时 10.0~10.1, 竞价冲到 11 — 竞价点必须仍在 0~H 内
  const host = render({
    rows: mkRows([10.0, 10.05, 10.1], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auction: { price: [['09:15:00', 10.2], ['09:20:00', 10.6], ['09:25:00', 11.0]] },
  })
  const auctionLine = polylines(host)[0]
  const ys = (auctionLine.getAttribute('points') ?? '').trim().split(/\s+/).map(p => Number(p.split(',')[1]))
  for (const y of ys) {
    expect(y).toBeGreaterThanOrEqual(0)
    expect(y).toBeLessThanOrEqual(80)
  }
})

it('量柱: 匹配量与未匹配量各画一组 rect (双层各锚带底/带顶, 末桶 clamp)', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    auction: {
      price: [['09:15:00', 10.0], ['09:20:00', 10.1], ['09:25:00', 10.1]],
      match: [['09:15:00', 100], ['09:25:00', 500]],
      unmatched: [['09:15:00', 300], ['09:25:00', -200]],
    },
  })
  // 默认 W=100 → paneW=29/B=10: 竞价栏 4 根量柱 (右侧另有分钟量柱, 用窄栏筛排除)
  expect(host.querySelectorAll('rect').length).toBeGreaterThanOrEqual(4)
  const gold = auctionBarsByFill(host, [GOLD], 100)
  const rg = auctionBarsByFill(host, [BULL, BEAR], 100)
  expect(gold).toHaveLength(2)
  expect(rg).toHaveLength(2)
  // 窄栏底色 #7A7A85 不参与量柱计数。
  // jsdom 只锁存在性与数值; 明暗两主题下 0.08 可辨性为设计声明的唯一目检项,
  // 属实现后待办: 需带竞价数据的应用实例截图复验, 不可辨升至 0.10 封顶
  // (组件 MiniIntraday.tsx 底色 rect 处有同一约定)。色彩合成估算 (非目检):
  // #7A7A85@0.08 在两主题 surface/base 上 ΔL*≈3.2~3.9, 均过 ~2-3 可辨阈
  expect(host.querySelectorAll(`rect[fill="${BG}"]`)).toHaveLength(1)
  expect(host.querySelector(`rect[fill="${BG}"]`)?.getAttribute('opacity')).toBe('0.08')
  // 方向性: 金柱自带底正立 (底边=H-3), 红绿柱自带顶倒挂 (顶边=bandTop)
  const { bandTop, bottomY } = bandGeom(56)
  for (const r of gold) expect(rectBottomY(r)).toBeCloseTo(bottomY, 5)
  for (const r of rg) expect(rectTopY(r)).toBeCloseTo(bandTop, 5)
  // 双层包围盒不相交 (中间 2px 空巷)
  expect(Math.min(...gold.map(rectTopY))).toBeGreaterThan(Math.max(...rg.map(rectBottomY)))
  // 数据末点 09:25:00 → 末桶 clamp: 柱中心=(B-0.5)/B×paneW
  const centers = [...gold, ...rg].map(rectCenterX)
  expect(centers.some(c => Math.abs(c - ((10 - 0.5) / 10) * 29) < 0.01)).toBe(true)
})

it('复权口径自动对齐: 分钟首根 open 与竞价末价不等时缩放竞价价格', () => {
  // 前复权比分时: 首根 open=9.0 (原始 10.0 的 0.9 倍), 竞价末价 10.0 → 缩放 0.9。
  // 若未缩放, 竞价线会明显高出分时线; 缩放后两者应几乎重合。
  const host = render({
    rows: mkRows([9.0, 9.05, 9.1], 9.0),
    prevClose: 9.0,
    width: 150,
    height: 80,
    auction: { price: [['09:15:00', 9.95], ['09:20:00', 10.0], ['09:25:00', 10.0]] },
  })
  const lines = polylines(host)
  const lastAuctionY = Number((lines[0].getAttribute('points') ?? '').trim().split(/\s+/).pop()?.split(',')[1])
  const firstMinuteY = Number((lines[2].getAttribute('points') ?? '').trim().split(/\s+/)[0]?.split(',')[1])
  // 竞价末价与开盘价是同一价位 → 两条线端点 y 接近 (放大后差 < 6px)
  expect(Math.abs(lastAuctionY - firstMinuteY)).toBeLessThan(6)
})

it('盘前无分钟K但已有竞价序列: 仍渲染竞价图', () => {
  const host = render({
    rows: [],
    prevClose: 10,
    width: 150,
    height: 80,
    auction: { price: [['09:15:00', 9.9], ['09:20:00', 10.05], ['09:25:00', 10.1]] },
  })
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('集合竞价')
  expect(polylines(host)).toHaveLength(1)
})

it('盘前既无分钟K也无竞价: 空占位', () => {
  const host = render({ rows: [], prevClose: 10, width: 150, height: 80 })
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('暂无分时')
})

it('竞价点不足 2 个: 退化为纯分时, 不留空栏', () => {
  const host = render({
    rows: mkRows([10.0, 10.1, 10.2], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auction: { price: [['09:25:00', 10.0]] },
  })
  expect(polylines(host)).toHaveLength(2)
  const price = polylines(host)[1].getAttribute('points') ?? ''
  expect(Number(price.trim().split(/\s+/)[0].split(',')[0])).toBe(0)
})

it('竞价末价低于昨收时竞价线涂绿 (低开: 竞价末价=开盘价)', () => {
  const host = render({
    rows: mkRows([9.8, 9.85], 9.8),
    prevClose: 10,
    auction: { price: [['09:15:00', 9.9], ['09:25:00', 9.8]] },
  })
  expect(polylines(host)[0].getAttribute('stroke')).toBe(BEAR)
})

it('竞价末价高于昨收时竞价线涂红 (高开: 竞价末价=开盘价)', () => {
  const host = render({
    rows: mkRows([10.2, 10.25], 10.2),
    prevClose: 10,
    auction: { price: [['09:15:00', 10.1], ['09:25:00', 10.2]] },
  })
  expect(polylines(host)[0].getAttribute('stroke')).toBe(BULL)
})

it('分桶: 60 均匀时间戳聚进 ≤30 根柱 (W=150→paneW44/B=15, 2×15=30 等号可达), 柱宽 ≥1.4px 且无圆角', () => {
  const times = Array.from({ length: 60 }, (_, i) => {
    const s = 33300 + Math.round((i * 600) / 59)
    return `09:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
  })
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auction: {
      price: [['09:15:00', 10.0], ['09:25:00', 10.1]],
      match: times.map((t, i) => [t, 100 + i] as [string, number]),
      unmatched: times.map((t, i) => [t, i % 2 ? -50 : 60] as [string, number]),
    },
  })
  const bars = [...auctionBarsByFill(host, [GOLD], 150), ...auctionBarsByFill(host, [BULL, BEAR], 150)]
  expect(bars.length).toBeGreaterThan(0)
  // 60 均匀点每桶必有值 → 恰 2×15=30; 必须用 ≤ (等号可达), 逐点画柱 (120 根) 会被抓住
  expect(bars.length).toBeLessThanOrEqual(30)
  for (const r of bars) {
    expect(Number(r.getAttribute('width'))).toBeGreaterThanOrEqual(1.4)
    expect(r.getAttribute('rx')).toBeNull()
  }
})

it('窄栏 snap: 同桶价格点 x 相等且=桶中心, 末点锁末桶 clamp (区分旧序号/连续映射)', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    auction: {
      price: [['09:15:03', 9.8], ['09:15:10', 10.4], ['09:25:00', 10.1]],
      match: [['09:15:05', 200]],
    },
  })
  const line = polylines(host)[0]
  const xs = lineXs(line)
  const ys = lineYs(line)
  // W=100 → paneW=29/B=10: 09:15:03 与 09:15:10 同在桶 0, x 相等且=(0.5/B)×paneW=1.45
  // (旧连续映射 0.145/0.483、旧序号 0 均在 ±0.5 之外); y 给明显差值 → 同桶竖直段可见
  expect(xs[0]).toBe(xs[1])
  expectNear(xs[0], (0.5 / 10) * 29)
  expect(xs[0]).toBeGreaterThan(1)
  expect(xs[0]).toBeLessThan(2)
  expect(Math.abs(ys[0] - ys[1])).toBeGreaterThan(1)
  // 09:25:00 → 末桶 clamp: x=(B-0.5)/B×paneW=27.55 (旧序号第 3 点=paneW=29, 未 clamp=30.45)
  expectNear(xs[2], ((10 - 0.5) / 10) * 29)
  expect(xs[2]).toBeGreaterThan(27)
  expect(xs[2]).toBeLessThan(28)
  // 与量柱对称: 同桶价格点 x = 柱 x (match 09:15:05 同在桶 0)
  const gold = barsByFill(host, [GOLD])
  expect(gold).toHaveLength(1)
  expect(rectCenterX(gold[0])).toBeCloseTo((0.5 / 10) * 29, 6)
})

it('固定轴按墙钟布点: 09:16:00 落在 15% 处而非 3 点中点 (50%)', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    auction: { price: [['09:15:00', 10.0], ['09:16:00', 10.05], ['09:25:00', 10.1]] },
  })
  const xs = lineXs(polylines(host)[0])
  // b(09:16:00)=floor(100/600×10)=1 → x=(1.5/10)×29=4.35=15% paneW (旧序号=50%)
  expect(xs[1]).toBeLessThan(29 * 0.2)
  expect(xs[1]).toBeGreaterThan(2)
})

it('盘前 AuctionOnly: 价格点吸附桶中心, 前密后疏第二点 x<W×20% (旧序号=50%)', () => {
  const host = render({
    rows: [],
    prevClose: 10,
    width: 146,
    height: 80,
    auction: {
      price: [['09:15:00', 9.9], ['09:15:50', 10.05], ['09:25:00', 10.1]],
      match: [['09:15:50', 300]],
    },
  })
  expect(host.querySelector('svg')?.getAttribute('aria-label')).toBe('集合竞价')
  const line = polylines(host)[0]
  const xs = lineXs(line)
  // W=146 → B=49: b(09:15:50)=floor(50/600×49)=4 → x=(4.5/49)×146≈13.41≈9.2% (旧序号中点=73)
  expect(xs[1]).toBeLessThan(146 * 0.2)
  expect(xs[1]).toBeGreaterThan(5)
  // 末点 09:25:00 → 末桶 clamp, 末点 circle 同映射
  const cx = Number(host.querySelector('circle')?.getAttribute('cx'))
  expect(cx).toBeCloseTo(((49 - 0.5) / 49) * 146, 6)
  // 与量柱对称: 同桶价格点 x = 柱 x
  const gold = barsByFill(host, [GOLD])
  expect(gold).toHaveLength(1)
  expectNear(rectCenterX(gold[0]), xs[1])
})

it('盘前脏时间戳: 尾部可解析的非标准格式照常用, 不可解析点跳过且无 NaN', () => {
  const host = render({
    rows: [],
    prevClose: 10,
    width: 146,
    height: 80,
    auction: {
      price: [
        ['9:15:30', 9.9],
        ['091500', 10.0],
        ['2026-10-19 09:15:40', 10.05],
        ['09:25:00', 10.1],
      ],
    },
  })
  const line = polylines(host)[0]
  const pts = (line.getAttribute('points') ?? '').trim().split(/\s+/)
  // '9:15:30'/'2026-10-19 09:15:40' 尾部可解析 → 保留; '091500' → 跳点
  expect(pts).toHaveLength(3)
  expect(pts.join(' ')).not.toContain('NaN')
})

it('全窗外回退: 三序列并集全在 14:57-15:00 → 数据轴铺满, 首末柱 x 差 ≥ paneW/2', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    auction: {
      price: [['14:57:00', 10.0], ['15:00:00', 10.1]],
      match: [['14:57:00', 100], ['15:00:00', 500]],
      unmatched: [['14:57:00', 200], ['15:00:00', -300]],
    },
  })
  const bars = [...auctionBarsByFill(host, [GOLD], 100), ...auctionBarsByFill(host, [BULL, BEAR], 100)]
  expect(bars.length).toBeGreaterThanOrEqual(2)
  const centers = bars.map(rectCenterX).sort((a, b) => a - b)
  // 数据轴 [14:57,15:00] → 首末桶中心 1.45/27.55, 差 26.1 ≥ paneW/2=14.5
  expect(centers[centers.length - 1] - centers[0]).toBeGreaterThanOrEqual(29 / 2)
})

it('部分窗外不回退 (既定行为): 窗外点 clamp 进首桶', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    auction: {
      price: [['09:15:00', 10.0], ['09:25:00', 10.1]],
      match: [['09:10:00', 100], ['09:12:00', 300]],
    },
  })
  // 并集含窗内点 → 不回退: 价格首点仍在首桶中心 (数据轴下会移到 b=3 → x=10.15)
  expectNear(lineXs(polylines(host)[0])[0], (0.5 / 10) * 29)
  // 09:10/09:12 (窗前) 均被 clamp 进首桶 → 1 根金柱, 中心=首桶中心
  const gold = barsByFill(host, [GOLD])
  expect(gold).toHaveLength(1)
  expect(rectCenterX(gold[0])).toBeCloseTo((0.5 / 10) * 29, 6)
})

it('双点同戳 (全窗外): (span)||1 兜底, 仍渲染不产生 NaN', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    auction: {
      price: [['14:57:00', 10.0], ['14:57:00', 10.1]],
      match: [['14:57:00', 100], ['14:57:00', 200]],
    },
  })
  const pts = (polylines(host)[0].getAttribute('points') ?? '').trim().split(/\s+/)
  expect(pts).toHaveLength(2)
  expect(pts.join(' ')).not.toContain('NaN')
  const gold = barsByFill(host, [GOLD])
  expect(gold).toHaveLength(1)
  expect(Number(gold[0].getAttribute('height'))).toBeGreaterThan(0)
  expect(Number(gold[0].getAttribute('x'))).toBeGreaterThanOrEqual(0)
})

it('单字段退化: 只配 match → 全金锚底, 占满整带', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    height: 80,
    auction: {
      price: [['09:15:00', 10.0], ['09:25:00', 10.1]],
      match: [['09:15:00', 100], ['09:25:00', 500]],
    },
  })
  expect(auctionBarsByFill(host, [BULL, BEAR], 100)).toHaveLength(0)
  const gold = auctionBarsByFill(host, [GOLD], 100)
  expect(gold).toHaveLength(2)
  const { volBandH, bottomY } = bandGeom(80)
  // 最大桶满格 = 占满整带 (无 2px 空巷分层); 全部自带底正立
  expect(Math.max(...gold.map(r => Number(r.getAttribute('height'))))).toBeCloseTo(volBandH, 5)
  for (const r of gold) expect(rectBottomY(r)).toBeCloseTo(bottomY, 5)
})

it('单字段退化: 只配 unmatched → 全红/绿锚带顶, 占满整带', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    height: 80,
    auction: {
      price: [['09:15:00', 10.0], ['09:25:00', 10.1]],
      unmatched: [['09:15:00', 300], ['09:25:00', -200]],
    },
  })
  expect(auctionBarsByFill(host, [GOLD], 100)).toHaveLength(0)
  const rg = auctionBarsByFill(host, [BULL, BEAR], 100)
  expect(rg).toHaveLength(2)
  const { volBandH, bandTop } = bandGeom(80)
  expect(Math.max(...rg.map(r => Number(r.getAttribute('height'))))).toBeCloseTo(volBandH, 5)
  for (const r of rg) expect(rectTopY(r)).toBeCloseTo(bandTop, 5)
})

// ── 栏宽三档 (R3): 档位只改窄栏占宽, 分桶数沿用按 paneW 自适应 ──

it('栏宽 wide 档 (W=150): auctionW=58/B=19, 竞价首末点=首/末桶中心, 连续竞价起点 plotX0=62', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auctionWidth: 'wide',
    auction: { price: [['09:15:00', 10.0], ['09:25:00', 10.1]] },
  })
  const auctionW = Math.max(24, Math.round((150 - 4) * AUCTION_WIDTH_RATIOS.wide))
  const B = Math.max(8, Math.min(60, Math.round(auctionW / 3)))
  expect(auctionW).toBe(58)
  expect(B).toBe(19)
  const xs = lineXs(polylines(host)[0])
  // 09:15:00 → 首 bucket 0; 09:25:00 → 末点 clamp 进末桶 18
  expectNear(xs[0], (0.5 / 19) * 58)
  expectNear(xs[1], ((19 - 0.5) / 19) * 58)
  const cont = lineXs(polylines(host)[polylines(host).length - 1])
  expect(cont[0]).toBe(58 + 4)
})

it('栏宽 narrow 档 (W=150): auctionW=29/B=10, 同构几何', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auctionWidth: 'narrow',
    auction: { price: [['09:15:00', 10.0], ['09:25:00', 10.1]] },
  })
  const auctionW = Math.max(24, Math.round((150 - 4) * AUCTION_WIDTH_RATIOS.narrow))
  const B = Math.max(8, Math.min(60, Math.round(auctionW / 3)))
  expect(auctionW).toBe(29)
  expect(B).toBe(10)
  const xs = lineXs(polylines(host)[0])
  expectNear(xs[0], (0.5 / 10) * 29)
  expectNear(xs[1], ((10 - 0.5) / 10) * 29)
  const cont = lineXs(polylines(host)[polylines(host).length - 1])
  expect(cont[0]).toBe(29 + 4)
})

it('栏宽 narrow 档 W=100: round(96×0.2)=19 被 24px 下限接管 → auctionW=24, 连续竞价起点 28', () => {
  const host = render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 100,
    height: 80,
    auctionWidth: 'narrow',
    auction: { price: [['09:15:00', 10.0], ['09:25:00', 10.1]] },
  })
  const cont = lineXs(polylines(host)[polylines(host).length - 1])
  expect(cont[0]).toBe(28)
  // 下限接管后 paneW=24 → B=8, 竞价首点仍在首桶中心
  const xs = lineXs(polylines(host)[0])
  expectNear(xs[0], (0.5 / 8) * 24)
})

it('standard 档显式传入与缺省等值 (回归锚: 默认档=现状 0.30)', () => {
  const mk = (auctionWidth?: AuctionBarWidth) => render({
    rows: mkRows([10.0, 10.1], 10.0),
    prevClose: 10,
    width: 150,
    height: 80,
    auction: { price: [['09:15:00', 10.0], ['09:25:00', 10.1]] },
    ...(auctionWidth ? { auctionWidth } : {}),
  })
  const xsExplicit = lineXs(polylines(mk('standard'))[0])
  const xsDefault = lineXs(polylines(mk())[0])
  expect(xsExplicit[0]).toBeCloseTo(xsDefault[0], 6)
  expect(xsExplicit[1]).toBeCloseTo(xsDefault[1], 6)
  // 默认档 W=150: auctionW=44 (与既有数值断言一致)
  expectNear(xsDefault[0], (0.5 / 15) * 44)
})
