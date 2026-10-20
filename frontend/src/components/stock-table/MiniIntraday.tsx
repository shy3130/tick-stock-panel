/** 迷你分时折线图（自选列表共享）。

用当日分钟K的 close 画一条折线 + 昨收水平基准线 + 分时均线。
风格仿同花顺/东方财富分时图：
- 价格折线：过昨收基准线红、不到绿、正好压线灰 — 与个股对话框 EChartsIntraday
  同一逻辑 (最后一根实时分钟 close vs 图中灰线基准), 颜色永远与线的位置一致
- 昨收基准线：浅灰实线（从开到右），比虚线更明显
- 分时均线：黄色细线（成交均价，这里用 close 的累计均值近似）
- 价格线下方渐变填充: 顶部半透明实色 → 底部全透明(与个股对话框 EChartsIntraday 一致)
空数据返回等尺寸占位 SVG，保证加载前后尺寸一致（同 MiniCandlestick 模式）。

纵轴模式 (表头「自适应/涨跌停」开关):
- adaptive (默认): 按实际价格区间, 与历史行为一致
- limit: 以昨收为轴心对称展示涨跌停带 (与详情图 EChartsIntraday 同口径),
  带宽由 limitRate 提供; 数据超出带 (无涨跌幅新股/异常) 时以实际范围为下限,
  不裁剪曲线

集合竞价叠加 (auction):
- 左侧窄栏 (宽度三档 auctionWidth: 窄20%/标准30%/宽40%, 默认标准) 画 9:15–9:25
  竞价价格轨迹, 与右侧连续竞价共用同一价格纵轴
  (昨收基准线贯穿两栏, 价格高低可直接横向比较)
- 固定墙钟轴 [09:15,09:25] 分桶: 盘中流式新增点不引起重排; 三序列有效点的
  并集整体在窗外时该次渲染回退数据轴铺满 (横跳至多一次)
- 配置了量字段或 volume 开启且有分钟量时整幅图上下分区 (仿同花顺详情分时):
  上部 ~64% 为价格区 (两栏共用同一 yScale, 对齐不破坏), 下部为独立量带 —
  价格线不再穿进量柱区。
  窄栏量带画竞价量双层: 匹配量(金色)自带底正立 + 未匹配量(红/绿=买卖剩余)
  自带顶倒挂, 中间 2px 空巷互不接触, 桶内聚合后每层独立归一; 量柱与价格点
  同用桶中心。右侧量带画分钟成交量柱 (按分钟涨跌红绿, 分桶防亚像素);
  分钟量柱仅 volume 开启时出现, 竞价量柱不受 volume 开关控制
- 只配价格无量字段且 volume 关 (或量数据缺失) 时, 保持全幅价格渲染 (与无竞价逐像素一致)
*/
import { useId } from 'react'
import type { MinuteKlineRow } from '@/lib/api'
import { AUCTION_WIDTH_RATIOS, type AuctionBarWidth } from '@/lib/list-columns'

export type MiniIntradayYMode = 'adaptive' | 'limit'

/** 竞价点列: [HH:MM:SS, value] */
export interface MiniIntradayAuctionSeries {
  price: [string, number][]
  /** 匹配量 (竞价成交量) */
  match?: [string, number][]
  /** 未匹配量 (正=买方剩余, 负=卖方剩余) */
  unmatched?: [string, number][]
}

/** 竞价墙钟窗 [09:15:00, 09:25:00] (当日秒数): 固定时间轴 */
const AUCTION_T0 = 9 * 3600 + 15 * 60
const AUCTION_T1 = 9 * 3600 + 25 * 60

/** 解析竞价时间戳尾部的 H:MM[:SS] 为当日秒 ('9:15:30'→33330, '2026-10-19 09:15:40'→33340,
 * '14:57'→53820)。只查格式不查时/分/秒范围 — '25:00:00'→90000 流入窗外机制, 永不 NaN;
 * 尾部不带该格式的脏时间戳 ('091500') 返回 null, 由调用方整点跳过。 */
function parseAuctionTime(t: string): number | null {
  const m = /(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(t)
  if (!m) return null
  return Number(m[1]) * 3600 + Number(m[2]) * 60 + (m[3] != null ? Number(m[3]) : 0)
}

/** 过滤后的竞价点 (t=当日秒, 按时间升序): 时间不可解析或数值非有限的点整点跳过 */
type AuctionPt = { t: number; v: number }

function filterAuctionPoints(series: [string, number][] | undefined | null): AuctionPt[] {
  const out: AuctionPt[] = []
  for (const [ts, v] of series ?? []) {
    const t = parseAuctionTime(ts)
    if (t != null && Number.isFinite(v)) out.push({ t, v })
  }
  return out.sort((a, b) => a.t - b.t)
}

interface AuctionAxis {
  /** 桶数 clamp(round(paneW/3), 8, 60) */
  B: number
  /** 每桶宽度 (paneW/B) */
  slot: number
  bucketIndex: (t: number) => number
  /** 桶中心 x: 量柱与价格点同用 (柱线逐点对齐) */
  xAt: (b: number) => number
}

/** 竞价时间轴 + 分桶: 固定墙钟轴 [09:15,09:25], 盘中流式新增点不引起重排 (与右侧
 * 连续竞价「当日从左往右填充」同语义); 三序列有效点的并集与窗无交集时该次渲染
 * 回退数据轴 [umin,umax] 铺满 (分母 (span)||1 防同戳除零; 固定轴↔数据轴横跳至多
 * 一次, 只影响窗外配置)。末点 t=t1 及窗外点 clamp 进首/末桶为既定行为。 */
function buildAuctionAxis(series: AuctionPt[][], paneW: number): AuctionAxis {
  const B = Math.max(8, Math.min(60, Math.round(paneW / 3)))
  let t0 = AUCTION_T0
  let t1 = AUCTION_T1
  let umin = Infinity
  let umax = -Infinity
  for (const pts of series) {
    for (const p of pts) {
      if (p.t < umin) umin = p.t
      if (p.t > umax) umax = p.t
    }
  }
  if (Number.isFinite(umin) && Number.isFinite(umax) && !(umin <= t1 && umax >= t0)) {
    t0 = umin
    t1 = umax
  }
  const span = (t1 - t0) || 1
  const bucketIndex = (t: number) => Math.min(B - 1, Math.max(0, Math.floor(((t - t0) / span) * B)))
  const xAt = (b: number) => ((b + 0.5) / B) * paneW
  return { B, slot: paneW / B, bucketIndex, xAt }
}

interface AuctionBar { x: number; y: number; w: number; h: number; fill: string; opacity: number }

/** 竞价量柱双层: 匹配量(金)自带底正立、未匹配量(红/绿=买卖剩余)自带顶倒挂, 各占
 * 量带一层、中间 2px 空巷互不接触; 只配一套时该套占满整带 (单字段退化)。
 * 桶内聚合: 匹配量取桶内 max|v|、未匹配量取桶内末值(末值符号定色); 每层按自身
 * 桶聚合的全局 max 独立归一 (两套量纲可差百倍)。注: 三序列来自用户自配扩展表
 * 字段, 累计/增量口径组件无从感知 — 累计口径下末桶恒满格是可预期视觉特征非 bug。 */
function buildAuctionBars(axis: AuctionAxis, match: AuctionPt[], unmatched: AuctionPt[], opts: {
  volBandH: number; bandTop: number; bottomY: number; gold: string; bull: string; bear: string
}): AuctionBar[] {
  const bars: AuctionBar[] = []
  const hasM = match.length > 0
  const hasU = unmatched.length > 0
  if (!hasM && !hasU) return bars
  const barW = axis.slot * 0.66
  // 双层: 匹配层锚带底、未匹配层锚带顶, 中间留 2px 空巷; 单字段退化时占满整带
  const matchLayerH = hasM ? (hasU ? Math.ceil(opts.volBandH / 2) : opts.volBandH) : 0
  const unmatchedLayerH = hasU ? (hasM ? opts.volBandH - 2 - matchLayerH : opts.volBandH) : 0
  if (hasM && matchLayerH > 0) {
    const agg = new Map<number, number>()  // 桶 → max|v|
    match.forEach(p => {
      const b = axis.bucketIndex(p.t)
      agg.set(b, Math.max(agg.get(b) ?? 0, Math.abs(p.v)))
    })
    let layerMax = 0
    agg.forEach(a => { layerMax = Math.max(layerMax, a) })
    layerMax = layerMax || 1
    agg.forEach((val, b) => {
      const h = (val / layerMax) * matchLayerH
      if (h <= 0) return  // 聚合值 0 桶跳过
      bars.push({ x: axis.xAt(b) - barW / 2, y: opts.bottomY - h, w: barW, h, fill: opts.gold, opacity: 0.85 })
    })
  }
  if (hasU && unmatchedLayerH > 0) {
    const agg = new Map<number, number>()  // 桶 → 末值 (点列已按时间升序, 后写覆盖)
    unmatched.forEach(p => agg.set(axis.bucketIndex(p.t), p.v))
    let layerMax = 0
    agg.forEach(v => { layerMax = Math.max(layerMax, Math.abs(v)) })
    layerMax = layerMax || 1
    agg.forEach((val, b) => {
      const h = (Math.abs(val) / layerMax) * unmatchedLayerH
      if (h <= 0) return
      bars.push({ x: axis.xAt(b) - barW / 2, y: opts.bandTop, w: barW, h, fill: val >= 0 ? opts.bull : opts.bear, opacity: 0.55 })
    })
  }
  return bars
}

/** 盘前无分钟K时的竞价单独渲染 (整幅宽度)。时间轴/量柱与主体窄栏共用同一套
 * (buildAuctionAxis/buildAuctionBars), 仅价格纵轴换成本图自身区间;
 * 配了量字段时同样上下分区 (价格上/量带下), 整幅无第二态, 不加窄栏底色。 */
function AuctionOnly({ rows, prevClose, match, unmatched, width, height }: {
  rows: AuctionPt[]
  prevClose?: number | null
  match?: [string, number][]
  unmatched?: [string, number][]
  width: number
  height: number
}) {
  const BULL = '#C74040'
  const BEAR = '#2D9B65'
  const FLAT = '#A1A1AA'
  const LINE_PREV_CLOSE = '#7A7A85'
  const LINE_MATCH = '#E0B84A'
  const padY = 3
  const W = width
  const H = height
  const last = rows[rows.length - 1].v
  const baseline = prevClose != null && prevClose > 0 ? prevClose : rows[0].v
  const color = last > baseline ? BULL : last < baseline ? BEAR : FLAT
  const values = rows.map(r => r.v)
  const hi = Math.max(...values, baseline)
  const lo = Math.min(...values, baseline)
  const range = hi - lo || 1
  // m/u 过滤落点 (rows 已由调用方以同一 filterAuctionPoints 过滤)
  const m = filterAuctionPoints(match)
  const u = filterAuctionPoints(unmatched)
  // 上下分区: 配了量字段时价格区止于 64%, 量带独立在底 (与主体同一比例)
  const hasVol = m.length > 0 || u.length > 0
  const priceBottom = hasVol ? Math.round(H * 0.64) : H - padY
  const y = (v: number) => padY + (1 - (v - lo) / range) * (priceBottom - padY)
  const bottomY = H - padY
  const volTop = hasVol ? priceBottom + 2 : bottomY
  const volBandH = bottomY - volTop
  const axis = buildAuctionAxis([rows, m, u], W)
  const bars = buildAuctionBars(axis, m, u, { volBandH, bandTop: volTop, bottomY, gold: LINE_MATCH, bull: BULL, bear: BEAR })
  // 价格点 x = 所在桶中心 (与量柱逐点对齐; 末点 circle 同映射)
  const xAtT = (t: number) => axis.xAt(axis.bucketIndex(t))
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" aria-label="集合竞价">
      <line x1={0} y1={y(baseline)} x2={W} y2={y(baseline)} stroke={LINE_PREV_CLOSE} strokeWidth={0.6} opacity={0.7} />
      {bars.map((b, i) => (
        <rect key={i} x={b.x} y={b.y} width={b.w} height={b.h} fill={b.fill} opacity={b.opacity} />
      ))}
      {hasVol && (
        <line x1={0} y1={volTop - 1} x2={W} y2={volTop - 1} stroke={LINE_PREV_CLOSE} strokeWidth={0.5} opacity={0.25} />
      )}
      <polyline
        points={rows.map(p => `${xAtT(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')}
        fill="none" stroke={color} strokeWidth={1.1} strokeLinejoin="round" strokeLinecap="round"
      />
      <circle cx={xAtT(rows[rows.length - 1].t)} cy={y(last)} r={1.4} fill={color} />
    </svg>
  )
}

export function MiniIntraday({ rows, prevClose, changePct, width = 100, height = 56, yMode = 'adaptive', limitRate = null, auction = null, volume = false, auctionWidth = 'standard' }: {
  rows: MinuteKlineRow[]
  /** 昨收价 (前收), 用于基准线。无则用 close/changePct 反算 */
  prevClose?: number | null
  /** 涨跌幅 (小数, 如 -0.029 = -2.9%)。仅用于 prevClose 缺失时反算基准线,
   *  不参与着色 — 着色恒以最后一根分钟 close 相对基准线的实时位置为准 */
  changePct?: number | null
  width?: number
  height?: number
  /** 纵轴模式: adaptive=实际价格区间, limit=按涨跌停带 */
  yMode?: MiniIntradayYMode
  /** 涨跌停幅度 (小数, 如 0.1)。null=无涨跌停带 (ETF/指数), limit 模式退化为自适应 */
  limitRate?: number | null
  /** 左侧集合竞价叠加 (价格必填, 量可选); 点数不足 2 时退化为整幅分时 */
  auction?: MiniIntradayAuctionSeries | null
  /** 成交量副图 (R1): 显示底部量带 (右侧分钟量柱)。竞价量柱画在量带内,
   *  数据存在时恒生效, 不受此开关控制 (R2) */
  volume?: boolean
  /** 竞价窄栏宽度档 (R3): 窄/标准/宽, 默认 'standard'≈30% 整图宽 */
  auctionWidth?: AuctionBarWidth
}) {
  const BULL = '#C74040'
  const BEAR = '#2D9B65'
  const FLAT = '#A1A1AA'              // 压线 (平盘): 灰, 与 EChartsIntraday 一致
  const LINE_PREV_CLOSE = '#7A7A85'   // 昨收基准线: 深灰实线
  const LINE_AVG = '#E0B84A'          // 均线: 暖黄

  const W = width
  const H = height
  const padY = 3

  // 盘前 (9:15–9:25) 还没有分钟K, 但竞价序列已有数据 — 此时单独渲染竞价栏,
  // 否则列表在集合竞价时段反而看不到任何东西。价格点列统一在此过滤
  // (脏时间戳/非有限值跳过), 盘前早退与下方主体窄栏共用同一结果。
  const auctionPriceRaw = filterAuctionPoints(auction?.price)
  if (!rows || rows.length < 2) {
    if (auctionPriceRaw.length < 2) {
      return <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block" aria-label="暂无分时" />
    }
    return <AuctionOnly rows={auctionPriceRaw} prevClose={prevClose} match={auction?.match} unmatched={auction?.unmatched} width={W} height={H} />
  }

  const n = rows.length

  const lastClose = rows[n - 1].close
  const firstRef = rows[0].open ?? rows[0].close

  // 昨收基准线 (0轴): 优先用 prevClose; 其次用 changePct 反算 (close/(1+changePct));
  // 最后回退到首根 open(为 null 时退首根 close)
  const baseline = (prevClose != null && prevClose > 0)
    ? prevClose
    : (changePct != null && changePct !== 0)
      ? lastClose / (1 + changePct)
      : firstRef

  // 涨跌着色: 最后一根实时分钟 close 相对图中灰线基准的位置 — 过基准线红、
  // 不到绿、正好压线灰, 与详情图 EChartsIntraday 同一逻辑。不用行数据的
  // changePct (策略运行时刻的冻结快照): 盘中股价穿过昨收后, 线已站上基准线
  // 却仍按旧快照涂绿, 与详情图和自身几何互相矛盾 (002469.SZ 2026-10-19 案例)。
  const color = lastClose > baseline ? BULL : lastClose < baseline ? BEAR : FLAT

  // 价格区间: close + 昨收 + 均线 全部纳入, 确保都在可视范围
  let hi = -Infinity, lo = Infinity
  // 累计均价 (分时均线的近似: close 的累计平均)
  const avgLine: number[] = []
  let cumSum = 0
  for (let i = 0; i < n; i++) {
    const c = rows[i].close
    cumSum += c
    const avg = cumSum / (i + 1)
    avgLine.push(avg)
    if (c > hi) hi = c
    if (c < lo) lo = c
    if (avg > hi) hi = avg
    if (avg < lo) lo = avg
  }

  // 竞价量点列 (与 auctionPriceRaw 同一过滤: 时间可解析 + 数值有限;
  // 价格点 <2 不足以成线 → 不显示窄栏)
  const auctionMatch = filterAuctionPoints(auction?.match)
  const auctionUnmatched = filterAuctionPoints(auction?.unmatched)
  const auctionActive = auctionPriceRaw.length >= 2
  // 上下分区 (价格区/量带分离) 启用条件: 竞价量数据存在 (auctionVolActive, 不受
  // volume 开关控制 — R2) 或 volume 开且分钟 K 有量 (rows 全 volume=0 时守卫拦下,
  // 不画空带); 两者都无时保持全幅价格渲染 (历史形态)
  const auctionVolActive = auctionActive && (auctionMatch.length > 0 || auctionUnmatched.length > 0)
  const volActive = auctionVolActive || (volume && rows.some(r => r.volume > 0))

  // 复权口径对齐: 分钟K可能是前复权 (除权日前后整体缩放), 竞价扩展表一般是原始价。
  // A股 9:25 集合竞价成交价即当日开盘价, 所以「首根分钟 open ÷ 竞价末价」=
  // 两套口径的缩放比例; 偏差 >0.1% 时才缩放 (日常交易日比例恒为 1, 不引入噪声)。
  const minOpen = rows[0].open ?? rows[0].close
  const auctionLastRaw = auctionPriceRaw.length ? auctionPriceRaw[auctionPriceRaw.length - 1].v : 0
  let auctionScale = 1
  if (auctionActive && minOpen > 0 && auctionLastRaw > 0) {
    const ratio = minOpen / auctionLastRaw
    // 0.3–3 覆盖分红/送转导致的复权差异; 超出视为「价格字段配错」(如配了涨幅%)
    if (ratio >= 0.3 && ratio <= 3 && Math.abs(ratio - 1) > 0.001) auctionScale = ratio
  }
  const auctionPrice = auctionScale === 1
    ? auctionPriceRaw
    : auctionPriceRaw.map(p => ({ t: p.t, v: p.v * auctionScale }))

  // 竞价价格也参与纵轴区间: 共用一把价格尺, 竞价跳空高开不能跑出画面
  if (auctionActive) {
    for (const p of auctionPrice) {
      if (p.v > hi) hi = p.v
      if (p.v < lo) lo = p.v
    }
  }

  // 把昨收也纳入区间
  hi = Math.max(hi, baseline)
  lo = Math.min(lo, baseline)

  // 涨跌停模式: 以昨收为轴心对称覆盖涨跌停带 (与详情图同口径)。数据超出带
  // (无涨跌幅新股/数据异常) 时以实际数据范围为下限 — 不能把曲线裁出图外。
  const limitBandActive = yMode === 'limit' && limitRate != null && limitRate > 0
  let bandUp = 0
  let bandDown = 0
  if (limitBandActive) {
    const limitDiff = baseline * limitRate
    const maxDiff = Math.max(limitDiff, hi - baseline, baseline - lo)
    hi = baseline + maxDiff
    lo = baseline - maxDiff
    bandUp = baseline + limitDiff
    bandDown = baseline - limitDiff
  }
  const range = hi - lo || 1

  // 上下分区 (仿同花顺详情分时): 配了量字段时价格区止于 ~64%, 下部为独立量带 —
  // 价格线不再穿进量柱区。两栏共用同一 yScale, 竞价/连续价格对齐在分区下依然精确成立
  const priceBottom = volActive ? Math.round(H * 0.64) : H - padY
  const yScale = (v: number) => padY + (1 - (v - lo) / range) * (priceBottom - padY)

  // 左右分栏: 竞价窄栏占 AUCTION_WIDTH_RATIOS[auctionWidth] 比例 (留 4px 间隔),
  // 其余给连续竞价。无竞价数据时右侧铺满整幅, 与历史渲染逐像素一致。
  const paneGap = auctionActive ? 4 : 0
  const auctionW = auctionActive ? Math.max(24, Math.round((W - paneGap) * (AUCTION_WIDTH_RATIOS[auctionWidth] ?? AUCTION_WIDTH_RATIOS.standard))) : 0
  const plotX0 = auctionW + paneGap
  const plotW = W - plotX0
  const xScale = (i: number, x0 = plotX0, w = plotW, count = n) => x0 + (i / Math.max(1, count - 1)) * w

  // 价格折线 points
  const pricePoints = rows.map((r, i) => `${xScale(i).toFixed(1)},${yScale(r.close).toFixed(1)}`).join(' ')
  // 均线 points
  const avgPoints = avgLine.map((v, i) => `${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`).join(' ')

  // 竞价时间轴 + 分桶 (buildAuctionAxis): 固定墙钟轴 [09:15,09:25], 三序列有效点
  // 并集整体在窗外时该次渲染回退数据轴。价格点 x = 所在桶中心, 与量柱逐点对齐
  // (首点不再贴 x=0, stroke 不再左溢; 代价: 同桶多价塌缩为竖直段)。
  const auctionAxis = auctionActive ? buildAuctionAxis([auctionPrice, auctionMatch, auctionUnmatched], auctionW) : null

  // 竞价价格折线与着色 (末点相对昨收, 与右侧同一规则)
  const auctionLast = auctionPrice.length ? auctionPrice[auctionPrice.length - 1].v : baseline
  const auctionColor = auctionLast > baseline ? BULL : auctionLast < baseline ? BEAR : FLAT
  const auctionPoints = auctionAxis
    ? auctionPrice
        .map(p => `${auctionAxis.xAt(auctionAxis.bucketIndex(p.t)).toFixed(1)},${yScale(p.v).toFixed(1)}`)
        .join(' ')
    : ''

  // 量带 (仅 volActive): 独立在价格区下方的底部条带, 与价格区以 2px 空隙相接
  const bottomY = H - padY
  const volTop = volActive ? priceBottom + 2 : bottomY
  const volBandH = volActive ? bottomY - volTop : 0
  // 竞价量柱: 双层 (匹配量金/自带底正立, 未匹配量红绿/自带顶倒挂, 2px 空巷),
  // 桶内聚合 + 每层独立归一 — 细节见 buildAuctionBars
  const auctionBars: AuctionBar[] = auctionAxis && volActive
    ? buildAuctionBars(auctionAxis, auctionMatch, auctionUnmatched, {
        volBandH, bandTop: volTop, bottomY, gold: LINE_AVG, bull: BULL, bear: BEAR,
      })
    : []
  // 右侧量带: 分钟成交量柱 (同花顺分时副图同构), 按分钟涨跌红绿、桶内取 max 量、
  // 末分钟涨跌定色; 分桶防亚像素 (plotW ~91px / 240 分钟 ≈ 0.4px/根)
  const minuteBars: AuctionBar[] = []
  if (volActive && n >= 2) {
    const B = Math.max(8, Math.min(120, Math.round(plotW / 2.5)))
    const slotM = plotW / B
    const bw = slotM * 0.66
    const agg = new Map<number, { max: number; up: boolean }>()
    for (let i = 0; i < n; i++) {
      const b = Math.min(B - 1, Math.floor((i / n) * B))
      const v = rows[i].volume
      const ref = i > 0 ? rows[i - 1].close : (rows[0].open ?? rows[0].close)
      const up = rows[i].close >= ref
      const cur = agg.get(b)
      if (!cur) agg.set(b, { max: v, up })
      else { if (v > cur.max) cur.max = v; cur.up = up }
    }
    let vmax = 0
    agg.forEach(a => { vmax = Math.max(vmax, a.max) })
    vmax = vmax || 1
    agg.forEach((a, b) => {
      if (a.max <= 0) return
      const h = (a.max / vmax) * volBandH
      minuteBars.push({
        x: plotX0 + (b + 0.5) * slotM - bw / 2, y: bottomY - h, w: bw, h,
        fill: a.up ? BULL : BEAR, opacity: 0.4,
      })
    })
  }

  // 渐变填充多边形 points: 价格折线 + 右下角 + 左下角, 闭合到价格区底 (渐变不进量带)
  const areaPoints = `${pricePoints} ${xScale(n - 1).toFixed(1)},${priceBottom.toFixed(1)} ${xScale(0).toFixed(1)},${priceBottom.toFixed(1)}`

  // 昨收参考线 y 坐标
  const prevCloseY = yScale(baseline)

  // 渐变 id 唯一化(自选列表同屏多张图, 避免互相覆盖)
  const gradId = useId().replace(/:/g, '')

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block">
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity={0.4} />
          <stop offset="1" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {/* 竞价窄栏底色 (最底层, 轻微区分两栏); 0.08 起步, 明暗两主题截图复验
          可辨性, 不可辨升至 0.10 封顶 — 全方案唯一只能目检的元素 */}
      {auctionActive && (
        <rect x={0} y={0} width={auctionW} height={H} fill={LINE_PREV_CLOSE} opacity={0.08} />
      )}
      {/* 昨收基准线 (深灰实线, 比虚线更明显); 有竞价栏时贯穿两栏, 价格可横向比较 */}
      <line
        x1={0} y1={prevCloseY} x2={W} y2={prevCloseY}
        stroke={LINE_PREV_CLOSE} strokeWidth={0.6} opacity={0.7}
      />
      {/* 竞价窄栏与连续竞价的分隔线 */}
      {auctionActive && (
        <line
          x1={plotX0 - paneGap / 2} y1={0} x2={plotX0 - paneGap / 2} y2={H}
          stroke={LINE_PREV_CLOSE} strokeWidth={0.5} opacity={0.25}
        />
      )}
      {/* 量带: 竞价量柱 (窄栏双层) + 分钟成交量柱 (右侧), 独立在价格区下方 */}
      {auctionBars.map((b, i) => (
        <rect key={`a${i}`} x={b.x} y={b.y} width={b.w} height={b.h} fill={b.fill} opacity={b.opacity} />
      ))}
      {minuteBars.map((b, i) => (
        <rect key={`m${i}`} x={b.x} y={b.y} width={b.w} height={b.h} fill={b.fill} opacity={b.opacity} />
      ))}
      {/* 价格区/量带分隔线 (贯穿整幅, 两栏借此粘合为一个整体) */}
      {volActive && (
        <line x1={0} y1={volTop - 1} x2={W} y2={volTop - 1} stroke={LINE_PREV_CLOSE} strokeWidth={0.5} opacity={0.25} />
      )}
      {/* 竞价价格折线 */}
      {auctionActive && (
        <polyline
          points={auctionPoints}
          fill="none"
          stroke={auctionColor}
          strokeWidth={1}
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity={0.9}
        />
      )}
      {/* 价格折线下方渐变填充 */}
      <polygon points={areaPoints} fill={`url(#${gradId})`} stroke="none" />
      {/* 涨跌停带边界 (仅 limit 模式): 淡红/淡绿虚线, 与详情图一致 */}
      {limitBandActive && (
        <>
          <line
            x1={plotX0} y1={yScale(bandUp)} x2={W} y2={yScale(bandUp)}
            stroke={BULL} strokeWidth={0.6} strokeDasharray="2 2" opacity={0.45}
          />
          <line
            x1={plotX0} y1={yScale(bandDown)} x2={W} y2={yScale(bandDown)}
            stroke={BEAR} strokeWidth={0.6} strokeDasharray="2 2" opacity={0.45}
          />
        </>
      )}
      {/* 分时均线 (暖黄细线) */}
      <polyline
        points={avgPoints}
        fill="none"
        stroke={LINE_AVG}
        strokeWidth={0.8}
        strokeLinejoin="round"
        strokeLinecap="round"
        opacity={0.85}
      />
      {/* 分时价格折线 */}
      <polyline
        points={pricePoints}
        fill="none"
        stroke={color}
        strokeWidth={1.1}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
