import { useEffect, useRef, useState } from 'react'

/**
 * 指标解释组件 + 术语字典。
 *
 * - 字典为纯数据（METRIC_TERM_LIST / METRIC_TERMS），可独立测试与复用。
 * - 组件沿用项目现有 ? 气泡惯例（点击开关、点外关闭、近右缘右对齐），
 *   与 StrategyBacktest 内 SharpeLabel 保持一致。
 */

export interface MetricTerm {
  /** 术语 key（英文小写下划线，供组件按 term 查询） */
  term: string
  /** 展示名（中文 + 英文缩写） */
  name: string
  /** 定义：这个指标在度量什么 */
  definition: string
  /** 方向：数值怎么读（越高越好 / 越低越好 / 视目标而定） */
  direction: string
  /** 使用告诫：什么时候会失真、不能怎么用 */
  caveat: string
}

export const METRIC_TERM_LIST: MetricTerm[] = [
  {
    term: 'psr',
    name: '概率夏普 (PSR)',
    definition: '在观测到的夏普与样本量下，策略真实超额收益大于零的概率。',
    direction: '越高越好，通常 >95% 才认为夏普较可信。',
    caveat: '只刻画单一策略夏普的估计不确定度，不纠正多重检验；换窗口或换样本结论可能翻转。',
  },
  {
    term: 'sharpe',
    name: '夏普比率 (Sharpe Ratio)',
    definition: '单位总波动换取的超额收益（年化收益扣除无风险利率后除以年化波动率）。',
    direction: '越高越好。',
    caveat: '短样本或交易次数少时容易偏高；收益分布非正态（尖峰厚尾）时会失真。',
  },
  {
    term: 'sortino',
    name: '索提诺比率 (Sortino Ratio)',
    definition: '只按下行波动计算的夏普：超额收益除以下行波动率。',
    direction: '越高越好。',
    caveat: '上行波动不计入分母，亏损样本少时分母不稳定；与夏普差异大时说明收益分布偏斜。',
  },
  {
    term: 'max_drawdown',
    name: '最大回撤 (Max Drawdown)',
    definition: '净值从历史峰值跌到随后谷底的最大跌幅。',
    direction: '绝对值越小越好。',
    caveat: '只反映区间内最深的一次回撤，不代表未来上限；对区间起点与单笔极端亏损敏感。',
  },
  {
    term: 'profit_factor',
    name: '利润因子 (Profit Factor)',
    definition: '全部盈利交易的总盈利除以全部亏损交易的总亏损。',
    direction: '大于 1 为盈利，越大越好。',
    caveat: '少数几笔大盈利会显著抬高；无亏损交易时不可计算，小样本下极不稳定。',
  },
  {
    term: 'payoff_ratio',
    name: '盈亏比 (Payoff Ratio)',
    definition: '平均每笔盈利金额除以平均每笔亏损金额。',
    direction: '越高越好，但需与胜率搭配解读。',
    caveat: '高盈亏比常伴随低胜率（趋势策略），单独看会误导；未实现平仓的交易口径不同不可比。',
  },
  {
    term: 'calmar',
    name: '卡玛比率 (Calmar Ratio)',
    definition: '年化收益除以最大回撤。',
    direction: '越高越好。',
    caveat: '分母是单次最深回撤，随机波动大；短区间年化外推会放大噪声。',
  },
  {
    term: 'information_ratio',
    name: '信息比率 (Information Ratio)',
    definition: '相对基准的超额收益除以跟踪误差，即承担单位偏离风险换来的主动收益。',
    direction: '越高越好。',
    caveat: '依赖基准选择与基准曲线覆盖度；基准数据缺失或错位时不可靠。',
  },
  {
    term: 'tracking_error',
    name: '跟踪误差 (Tracking Error)',
    definition: '策略收益相对基准收益之差的波动率（年化）。',
    direction: '视目标而定：指数增强类越低越好，主动策略高偏离是预期内。',
    caveat: '只度量偏离幅度，不区分跑赢还是跑输；基准覆盖不足时被低估。',
  },
  {
    term: 'alpha',
    name: '阿尔法 (Alpha)',
    definition: '回归口径下剥离基准涨跌（Beta）后剩余的策略超额收益。',
    direction: '大于零说明有基准外的正贡献。',
    caveat: '依赖基准与 Beta 的估计，模型设定（频率、区间）变化会明显改变数值。',
  },
  {
    term: 'beta',
    name: '贝塔 (Beta)',
    definition: '策略收益对基准涨跌的敏感度：基准每变动 1%，策略平均变动 Beta%。',
    direction: '视目标而定：≈1 同步市场，对冲策略看偏离程度。',
    caveat: '回归估计有噪声，极端行情下关系会漂移；不代表因果关系。',
  },
  {
    term: 'var',
    name: '风险价值 (VaR 5%)',
    definition: '在 5% 的坏情形分位上，单期可能损失的最大幅度。',
    direction: '绝对值越小越好。',
    caveat: '不描述超过分位后的尾部损失有多深；样本外被突破是正常现象，不是失效证明。',
  },
  {
    term: 'cvar',
    name: '条件风险价值 (CVaR / Expected Shortfall)',
    definition: '最差 5% 情形的平均损失，即尾部损失的期望。',
    direction: '绝对值越小越好。',
    caveat: '依赖左尾样本量，小样本下高估或低估都可能；对单笔极端亏损极敏感。',
  },
  {
    term: 'expectancy',
    name: '期望值 (Expectancy)',
    definition: '平均每笔交易的期望收益：胜率×平均盈利 − 败率×平均亏损。',
    direction: '大于零长期才能盈利，越大越好。',
    caveat: '历史期望不代表未来分布；重尾交易（偶发巨亏）会让算术期望失真。',
  },
  {
    term: 'mae',
    name: '最大不利偏移 (MAE)',
    definition: '持仓期间日 K 最低价相对入场价的最大不利偏移，≤0。',
    direction: '绝对值越小越好，用于评估止损宽度。',
    caveat: '基于日 K 日内区间的诊断量，不代表可成交实现的损失；口径随建仓方式不同。',
  },
  {
    term: 'mfe',
    name: '最大有利偏移 (MFE)',
    definition: '持仓期间日 K 最高价相对入场价的最大有利偏移，≥0。',
    direction: '越大说明浮盈空间越大，用于评估止盈位置。',
    caveat: '观察到的最高价不代表能以该价成交退出；未考虑其间波动路径。',
  },
  {
    term: 'dsr',
    name: '紧缩夏普 (Deflated Sharpe Ratio)',
    definition: '对多重检验校正后的夏普显著性：尝试的组合越多，门槛越高。',
    direction: '越高越好，校正后仍显著说明不是挑出来的运气。',
    caveat: '需要试验次数与方差信息，仅针对已尝试的组合校正；未尝试的搜索空间不在校正内。',
  },
  {
    term: 'pbo',
    name: '过拟合概率 (PBO)',
    definition: '训练期排名靠前的组合在留出期跌出排名的概率（回测过拟合概率）。',
    direction: '越低越好，接近 0.5 说明训练排名与留出表现无关。',
    caveat: '受分块方式与样本量影响，是概率诊断不是判决；组合数少时估计粗糙。',
  },
  {
    term: 'capacity_utilization',
    name: '容量利用率 (Capacity Utilization)',
    definition: '策略成交额占标的日均成交额的占比，度量冲击成本与容量上限。',
    direction: '越低越好，占比高说明容量接近极限。',
    caveat: '以历史成交额近似；流动性骤降（停牌、极端行情）时真实容量远小于估计。',
  },
  {
    term: 'omega',
    name: 'Omega 比率 (Omega Ratio)',
    definition: '收益超过与低于给定阈值部分的概率加权之比。',
    direction: '越高越好。',
    caveat: '对阈值选择敏感，阈值不同结论可能不同；小样本尾部权重不稳定。',
  },
  {
    term: 'win_rate',
    name: '胜率 (Win Rate)',
    definition: '盈利交易笔数占总平仓笔数的比例。',
    direction: '高胜率不等于赚钱，需与盈亏比同看。',
    caveat: '止盈止损结构决定分布形态；只看胜率会系统性低估趋势策略价值。',
  },
  {
    term: 'annual_volatility',
    name: '年化波动率 (Annual Volatility)',
    definition: '日收益标准差按交易期数缩放的年化值。',
    direction: '同等收益下越低越好。',
    caveat: '用日收益独立同分布假设缩放；存在自相关或跳跃时会被低估。',
  },
  {
    term: 'total_return',
    name: '累计收益 (Total Return)',
    definition: '回测区间内策略净值的总涨跌幅：期末权益 ÷ 期初资金 − 1，已计入交易成本。',
    direction: '越高越好，但需结合区间长度与最大回撤一起看。',
    caveat: '不做年化、不能线性外推；区间起止选择对数值影响极大，短区间显得特别猛或特别惨。',
  },
  {
    term: 'annual_return',
    name: '年化收益 (Annualized Return)',
    definition: '把区间累计收益按复利折算成一年期的收益率（几何年化）。',
    direction: '越高越好。',
    caveat: '短区间年化是剧烈外推：三个月赚 10% 就年化约 48%；样本不足时年化基本没有参考意义。',
  },
  {
    term: 'benchmark_return',
    name: '同期基准收益 (Benchmark Return)',
    definition: '同一区间内基准指数（默认上证指数）的涨跌幅，作为策略表现的对照。',
    direction: '视目标而定：主动策略的意义在于跑赢基准。',
    caveat: '基准曲线覆盖不足或起点错位时对比失真；换基准会改变“超额”的含义，前后结果不可直接比。',
  },
  {
    term: 'excess_return',
    name: '超额收益 (Excess Return)',
    definition: '策略累计收益减去同期基准收益的差值。',
    direction: '大于零说明区间内跑赢基准。',
    caveat: '算术差不含波动调整；牛市跑输、熊市跑赢都常见，需结合 Beta 与信息比率解读。',
  },
  {
    term: 'trade_count',
    name: '交易笔数 (Trade Count)',
    definition: '回测区间内已平仓的完整交易笔数（未平仓持仓不计入）。',
    direction: '本身无好坏，是其他交易统计可信度的“样本量”。',
    caveat: '笔数太少（如不足 30 笔）时胜率、盈亏比等统计噪声极大；换参数让笔数暴增通常意味着过度拟合。',
  },
  {
    term: 'final_equity',
    name: '最终权益 (Final Equity)',
    definition: '回测结束日账户总权益（现金 + 持仓市值），等于初始资金 ×（1 + 累计收益）。',
    direction: '越高越好，但绝对值取决于初始资金规模。',
    caveat: '是账面市值口径而非可提取现金，含未平仓持仓的浮动盈亏。',
  },
  {
    term: 'tail_ratio',
    name: '尾部比率 (Tail Ratio)',
    definition: '收益分布最好 5% 与最差 5% 分位数之比，衡量极端好日子与极端坏日子的强弱对比。',
    direction: '越大越好，大于 1 说明上尾强于下尾。',
    caveat: '两端各只有 5% 样本，小样本下极不稳定；对单笔极端行情高度敏感。',
  },
  {
    term: 'recovery_factor',
    name: '恢复因子 (Recovery Factor)',
    definition: '累计净收益 ÷ 最大回撤，衡量总收益“赚回”一次最深亏损的能力。',
    direction: '越大越好，大于 1 说明总收益超过历史最深回撤。',
    caveat: '分母是单次最深回撤，随机性大；它不反映回撤修复花了多久，也不保证未来回撤更浅。',
  },
  {
    term: 'downside_volatility',
    name: '下行波动率 (Downside Volatility)',
    definition: '只用亏损期收益计算的年化波动率，盈利期的波动不计入分母。',
    direction: '同等收益下越低越好。',
    caveat: '依赖负收益样本，无亏损样本时不可算；与总波动差异大说明收益分布明显偏斜。',
  },
  {
    term: 'ulcer_index',
    name: '溃疡指数 (Ulcer Index)',
    definition: '把每个时点的回撤深度平方后取平均再开方，同时反映回撤深度与持续时间。',
    direction: '越低越好。',
    caveat: '平方放大深回撤权重，对区间长度敏感；与最大回撤口径不同，两者数值不可直接比较。',
  },
  {
    term: 'capacity_multiple',
    name: '容量倍数 (Capacity Multiple)',
    definition: '线性外推估算：当前资金约可放大的倍数，此时 p90 笔成交仍不触碰量能上限。',
    direction: '越大越好，说明策略能承接更大规模资金。',
    caveat: '假设成交价与滚动量能不随资金规模变化，且未计多笔同日抢同一上限的挤占；是近似而非精确容量解。',
  },
  {
    term: 'listing_age_gate',
    name: '上市天数门控 (Listing Age Gate)',
    definition: '上市不足 N 天的标的整段不进入回测面板（删行口径，非入场时点过滤）。',
    direction: '视研究目的而定：过滤次新股可剔除上市初期高波动、无涨跌幅限制阶段。',
    caveat: '上市日期缺失的行按 fail-open 保留；门控改变样本构成，与未开启的结果不可直接对比。',
  },
  {
    term: 'monte_carlo_trade_shuffle',
    name: '蒙特卡洛重排 (Monte Carlo Trade Shuffle)',
    definition: '把同一批逐笔收益随机打乱成交顺序重排多次，看终值与最大回撤对“顺序运气”的敏感度。',
    direction: '重排回撤比实际更差的概率越低，说明实际回撤不依赖特定成交顺序，越稳健。',
    caveat: '只重排顺序、不改变收益集合（终值均值收敛于全排列复合收益）；不模拟资金占用与并发持仓，是交易级诊断而非账户级蒙特卡洛，不能与账户净值曲线直接比较。',
  },
]

/** term → 词条 查询表（由列表构建，天然无重复 key） */
export const METRIC_TERMS: Record<string, MetricTerm> = Object.fromEntries(
  METRIC_TERM_LIST.map(item => [item.term, item]),
)

/**
 * 指标解释 ? 气泡：点击开关、点击外部关闭、靠近右缘时右对齐。
 * term 不在字典中时渲染 null，调用方无需预判。
 */
export function MetricExplainer({ term, className = '' }: { term: string; className?: string }) {
  const [open, setOpen] = useState(false)
  const [alignRight, setAlignRight] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)
  const entry = METRIC_TERMS[term]

  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  if (!entry) return null

  const toggle = () => {
    if (!open && ref.current) {
      const rect = ref.current.getBoundingClientRect()
      setAlignRight(rect.left + 256 > window.innerWidth)
    }
    setOpen(o => !o)
  }

  return (
    <span className={`relative inline-flex items-center ${className}`} ref={ref}>
      <button
        type="button"
        onClick={toggle}
        aria-label={`${entry.name} 解释`}
        aria-expanded={open}
        className="inline-flex h-3 w-3 shrink-0 items-center justify-center rounded-full border border-border bg-base text-[9px] leading-none text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        ?
      </button>
      {open && (
        <span className={`absolute top-full z-50 mt-1.5 block w-64 max-w-[calc(100vw-1.5rem)] rounded-lg border border-border bg-elevated px-3 py-2.5 text-left text-[11px] leading-relaxed text-secondary shadow-xl ${alignRight ? 'right-0' : 'left-0'}`}>
          <span className="block font-medium text-foreground">{entry.name}</span>
          <span className="mt-1 block">{entry.definition}</span>
          <span className="mt-0.5 block text-secondary">{entry.direction}</span>
          <span className="mt-0.5 block text-warning">{entry.caveat}</span>
        </span>
      )}
    </span>
  )
}
