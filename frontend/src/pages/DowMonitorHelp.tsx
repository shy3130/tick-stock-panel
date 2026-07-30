import {
  Activity,
  ArrowLeft,
  BarChart3,
  BookOpenCheck,
  Gauge,
  ShieldAlert,
  TrendingUp,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { PageHeader } from '@/components/PageHeader'
import { INTERPRETATION_THRESHOLDS } from '@/components/dow-monitor/interpretationMarketContext'
import { cn } from '@/lib/cn'

type HelpMarket = 'cn' | 'hk' | 'us'
type MetricMode = 'live' | 'stable'

const MARKET_LABELS: Record<HelpMarket, string> = {
  cn: 'A股',
  hk: '港股',
  us: '美股',
}

const SECTIONS = [
  { id: 'key-interpretation', label: '重点解读' },
  { id: 'quick-start', label: '快速决策路径' },
  { id: 'trend-position', label: '趋势 / 位置' },
  { id: 'momentum-speed', label: '动能 / 涨速' },
  { id: 'volume-funds', label: '量价 / 资金' },
  { id: 'breakout-risk', label: '突破 / 风险' },
  { id: 'sudden-anomaly', label: '突发异动高亮' },
  { id: 'scenarios', label: '典型组合场景' },
  { id: 'quick-reference', label: '指标速查表' },
] as const

const REFERENCE_ROWS = [
  ['通道', '稳', '趋势方向更明确', '—', '--', '不能单独决定买卖'],
  ['控制', '稳', '位于控制线上方更远', '贴近稳定控制线', '--', '不能替代正式信号'],
  ['VWAP', '稳', '高于当日成交量加权均价', '接近当日VWAP', '--', '不是用户持仓成本'],
  ['日内位置', '实时', '更靠近日内高位', '约50表示日内中部', '--', '位置不等于方向'],
  ['1m', '实时', '当前分钟向上加速', '当前分钟变化较小', '--', '不能升级正式信号'],
  ['5m / 15m', '稳', '完成K线动能向上', '完成K线变化较小', '--', '不能代表未来必然同向'],
  ['量比', '稳', '稳定周期成交更活跃', '—', '--', '放量不等于上涨'],
  ['量速', '实时', '当前分钟成交加速', '—', '--', '容易受分钟初段影响'],
  ['资金流入', '稳', '完整资金中流入占比更高', '接近流入流出均衡', '未确认', '不是逐笔主动买入占比'],
  ['五档', '实时', '买盘挂单相对更强', '买卖挂单接近平衡', '--', '挂单可以快速撤销'],
  ['高', '实时', '距离日高更远', '更靠近日内高点', '--', '接近日高不等于突破'],
  ['低', '实时', '距离日低更远', '更靠近日内低点', '--', '接近日低不等于破位'],
  ['ATR14', '稳', '15m短线波动风险更高', '—', '--', '不表示涨跌方向'],
  ['振幅/ATR', '实时+稳', '当日振幅已消耗更多常态波动', '—', '--', '不表示剩余空间必然耗尽'],
  ['周期确认', '稳', '更多稳定周期同向', '—', '--', '方向仍看正式信号'],
  ['数据时效', '实时', '字段源时间更接近当前', '—', '--', '超过90秒自动弱化'],
] as const

function normalizeMarket(value: string | null): HelpMarket {
  return value === 'cn' || value === 'us' ? value : 'hk'
}

function ModeBadge({ mode }: { mode: MetricMode }) {
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-full border px-1.5 text-[10px] font-semibold',
        mode === 'live'
          ? 'border-cyan-400/30 bg-cyan-400/10 text-cyan-300'
          : 'border-border bg-elevated text-muted',
      )}
    >
      {mode === 'live' ? '实时' : '稳'}
    </span>
  )
}

function MetricItem({
  name,
  mode,
  summary,
  reading,
  caution,
}: {
  name: string
  mode: MetricMode
  summary: string
  reading: string
  caution: string
}) {
  return (
    <article className="rounded-lg border border-border/80 bg-elevated/40 p-3.5">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-foreground">{name}</h3>
        <ModeBadge mode={mode} />
      </div>
      <p className="mt-2 text-xs leading-5 text-muted">{summary}</p>
      <p className="mt-2 text-xs leading-5 text-foreground/90">
        <strong className="font-semibold">怎么看：</strong>
        {reading}
      </p>
      <p className="mt-1 text-xs leading-5 text-muted">
        <strong className="font-semibold text-warning">避免误读：</strong>
        {caution}
      </p>
    </article>
  )
}

function GuideSection({
  id,
  title,
  question,
  icon,
  children,
}: {
  id: string
  title: string
  question: string
  icon: ReactNode
  children: ReactNode
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-title`}
      className="scroll-mt-20 rounded-xl border border-border bg-surface p-4 shadow-sm sm:p-5"
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent">
          {icon}
        </span>
        <div>
          <h2 id={`${id}-title`} tabIndex={-1} className="text-base font-semibold text-foreground outline-none">
            {title}
          </h2>
          <p className="mt-1 text-xs leading-5 text-muted">{question}</p>
        </div>
      </div>
      <div className="mt-4">{children}</div>
    </section>
  )
}

export function DowMonitorHelp() {
  const [searchParams] = useSearchParams()
  const market = normalizeMarket(searchParams.get('market'))

  return (
    <main
      data-testid="dow-monitor-help-page"
      className="min-h-full min-w-0 overflow-x-clip bg-base"
    >
      <PageHeader
        title="趋势监控指标说明"
        subtitle={`当前市场：${MARKET_LABELS[market]} · 先看正式信号，再判断趋势、动能、量价与执行风险`}
        right={(
          <Link
            to={`/dow-monitor?market=${market}`}
            aria-label="返回趋势监控"
            className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-muted transition-colors hover:bg-elevated hover:text-foreground"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
            返回趋势监控
          </Link>
        )}
      />

      <div className="mx-auto w-full max-w-[1440px] px-3 py-4 sm:px-5 sm:py-6">
        <div className="rounded-xl border border-accent/20 bg-accent/5 p-4 sm:p-5">
          <div className="flex items-start gap-3">
            <BookOpenCheck className="mt-0.5 h-5 w-5 shrink-0 text-accent" aria-hidden="true" />
            <div>
              <p className="text-sm font-semibold text-foreground">先分清“正式信号”和“观察指标”</p>
              <p className="mt-1 text-xs leading-5 text-muted">
                正式买卖信号来自后端完成K线和持久化通知。页面中的实时指标用于观察盘中变化，
                实时指标不能生成、翻转或升级正式信号。
              </p>
              <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted">
                <span className="inline-flex items-center gap-1.5"><ModeBadge mode="live" />盘中观察，可能快速变化</span>
                <span className="inline-flex items-center gap-1.5"><ModeBadge mode="stable" />完成K线或后端决策</span>
                <span><strong className="text-foreground">--</strong> 表示缺失或不满足稳定条件，不表示 0</span>
              </div>
            </div>
          </div>
        </div>

        <div className="mt-4 grid min-w-0 gap-4 lg:grid-cols-[220px_minmax(0,1fr)]">
          <aside className="min-w-0">
            <nav
              aria-label="指标说明目录"
              className="overflow-x-auto rounded-xl border border-border bg-surface p-2 lg:sticky lg:top-4 lg:overflow-visible"
            >
              <div className="flex min-w-max gap-1 lg:min-w-0 lg:flex-col">
                {SECTIONS.map((section, index) => (
                  <a
                    key={section.id}
                    href={`#${section.id}`}
                    className="flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-xs text-muted transition-colors hover:bg-elevated hover:text-foreground"
                  >
                    <span aria-hidden="true" className="text-[10px] tabular-nums text-muted/70">
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    {section.label}
                  </a>
                ))}
              </div>
            </nav>
          </aside>

          <div className="min-w-0 space-y-4">
            <GuideSection
              id="key-interpretation"
              title="重点解读"
              question="把分散指标转换成一条可核验的市场结论：先看发生了什么，再看为什么，最后核对关键价。"
              icon={<BookOpenCheck className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="grid gap-3 xl:grid-cols-3">
                {[
                  ['第一行：结论', '显示机会、风险、异动、观察或数据状态，以及当前最重要的走势形态。'],
                  ['第二行：市场行为', '解释价格、趋势、量能、资金与盘口如何共同形成当前结论，不重复罗列指标。'],
                  ['第三行：关键价', '给出确认、失效、压力、日高日低、VWAP或趋势控制线等可核对价格。'],
                ].map(([title, copy]) => (
                  <article key={title} className="rounded-lg border border-border bg-elevated/40 p-3.5">
                    <h3 className="text-sm font-semibold text-foreground">{title}</h3>
                    <p className="mt-2 text-xs leading-5 text-muted">{copy}</p>
                  </article>
                ))}
              </div>

              <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
                {[
                  ['机会', '价格结构与至少一个独立维度形成同向支持。'],
                  ['风险', '价格结构和动能、量能或卖压共同指向下行或失效。'],
                  ['异动', '某项实时证据突变，但价格或其他维度尚未确认。'],
                  ['观察', '周期冲突或证据不足，暂无清晰机会。'],
                  ['数据', '行情或K线延迟，暂停产生新的实时解读。'],
                ].map(([title, copy]) => (
                  <article key={title} className="rounded-lg border border-border bg-base/40 p-3">
                    <h3 className="text-xs font-semibold text-foreground">{title}</h3>
                    <p className="mt-1 text-[11px] leading-5 text-muted">{copy}</p>
                  </article>
                ))}
              </div>

              <div className="mt-3 space-y-3 text-xs leading-5 text-muted">
                <article className="rounded-lg border border-accent/20 bg-accent/5 p-3.5">
                  <h3 className="font-semibold text-foreground">正在尝试与已确认</h3>
                  <p className="mt-1">
                    “正在尝试”使用实时价格判断是否越过结构位；“已确认”只接受最近一根已完成5分钟K线的收盘结果。
                    近60分钟区间取最近12根已完成5分钟K线，形成中的K线和前一交易日数据不参与确认。
                  </p>
                </article>

                <article className="rounded-lg border border-border bg-elevated/40 p-3.5">
                  <h3 className="font-semibold text-foreground">关键参考价从哪里来</h3>
                  <p className="mt-1">
                    日高、日低描述当天实时边界；参考高低来自当天已完成5分钟K线；近60分钟区间用于判断突破或破位；
                    VWAP表示当日成交量加权均价；趋势线采用稳定的15m或30m控制线。缺少可靠价格时显示“关键价待确认”。
                  </p>
                </article>

                <article className="rounded-lg border border-border bg-elevated/40 p-3.5">
                  <h3 className="font-semibold text-foreground">确认价与失效价</h3>
                  <p className="mt-1">
                    确认价表示完成5分钟K线需要站上或跌破的结构位；失效价表示原判断不再成立的边界。
                    两者可能是同一条区间边界，但比较方向相反，必须结合“5m收&gt;”或“5m收&lt;”阅读。
                  </p>
                </article>

                <article className="rounded-lg border border-border bg-elevated/40 p-3.5">
                  <h3 className="font-semibold text-foreground">组合门槛与盘口限制</h3>
                  <p className="mt-1">
                    机会或明确风险至少需要两个独立维度。量能达到
                    {INTERPRETATION_THRESHOLDS.volumeRatio}倍可构成量能证据；资金流入达到
                    {INTERPRETATION_THRESHOLDS.fundsUpPct}%可构成向上资金证据；盘口压力达到正负
                    {INTERPRETATION_THRESHOLDS.depthUpPct}%可构成盘口方向证据。
                    盘口挂单可快速撤销，因此盘口单项异常不能单独生成机会或明确风险，只能标记“异动待确认”。
                  </p>
                </article>

                <article className="rounded-lg border border-warning/30 bg-warning/5 p-3.5">
                  <h3 className="font-semibold text-foreground">与正式买卖信号的边界</h3>
                  <p className="mt-1">
                    重点解读是基于现有指标的确定性市场解释，不会生成、翻转或升级后端正式买卖信号，
                    也不是买卖建议或自动交易指令。操作前仍应核对正式信号、北京时间、流动性和个人风险限制。
                  </p>
                </article>
              </div>
            </GuideSection>

            <GuideSection
              id="quick-start"
              title="快速决策路径"
              question="盯盘时按固定顺序阅读，避免被单个快速变化的数字带着走。"
              icon={<Gauge className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <ol className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                {[
                  ['1', '看正式信号', '先确认是否已有后端方向和发生时间。'],
                  ['2', '看趋势位置', '判断大方向，以及价格处在控制线、VWAP和日内区间的什么位置。'],
                  ['3', '看动能量价', '判断短线是否同向，成交和盘口是否配合。'],
                  ['4', '看突破风险', '判断距离关键位置、波动风险和稳定周期确认。'],
                ].map(([step, title, copy]) => (
                  <li key={step} className="rounded-lg border border-border bg-elevated/40 p-3">
                    <span className="text-[10px] font-semibold text-accent">STEP {step}</span>
                    <p className="mt-1 text-sm font-semibold text-foreground">{title}</p>
                    <p className="mt-1 text-xs leading-5 text-muted">{copy}</p>
                  </li>
                ))}
              </ol>
            </GuideSection>

            <GuideSection
              id="trend-position"
              title="趋势 / 位置"
              question="回答：现在是什么方向，价格处在稳定趋势线、VWAP和日内区间的什么位置？"
              icon={<TrendingUp className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <MetricItem
                  name="通道"
                  mode="stable"
                  summary="比较最近完成的15m、30m K线均线排列，显示上升、下降、震荡/过渡或待确认。"
                  reading="两个稳定周期同向时，趋势结构比单一短周期更明确。"
                  caution="通道描述方向，不等于现在就是买点或卖点。"
                />
                <MetricItem
                  name="控制"
                  mode="stable"
                  summary="价格相对稳定控制线的百分比距离，只按15m → 30m → 缺失回退，永不使用5m。"
                  reading="正负表示价格位于控制线哪一侧，绝对值表示距离。"
                  caution="必须结合通道和正式信号理解；--不是0%。"
                />
                <MetricItem
                  name="VWAP"
                  mode="stable"
                  summary="同时展示当日成交量加权均价和现价相对VWAP的偏离百分比。"
                  reading="正值表示现价高于VWAP，负值表示低于VWAP。"
                  caution="VWAP是市场日内成交基准，不是用户持仓成本，也不代表VWAP自身正在上升或下降。"
                />
                <MetricItem
                  name="日内位置"
                  mode="live"
                  summary="按（现价－日低）÷（日高－日低）计算，并限制在0到100。"
                  reading="接近0表示靠近日内低位，接近100表示靠近日内高位，约50表示区间中部。"
                  caution="日内位置只描述所处区间，不代表接下来一定向高点或低点移动。"
                />
              </div>
            </GuideSection>

            <GuideSection
              id="momentum-speed"
              title="动能 / 涨速"
              question="回答：当前分钟是否加速，完成K线的短线动能是否同向？"
              icon={<Activity className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="grid gap-3 xl:grid-cols-3">
                <MetricItem
                  name="1m"
                  mode="live"
                  summary="当前1分钟K线从开盘到现价的变化百分比。"
                  reading="正值表示当前分钟向上加速，负值表示当前分钟向下加速。"
                  caution="1m会快速变化，只是盘中观察，不能改变正式信号。"
                />
                <MetricItem
                  name="5m"
                  mode="stable"
                  summary="最近两根完成5m K线收盘价的变化百分比。"
                  reading="与1m同向时说明短线加速获得完成K线支持。"
                  caution="单一5m动能不能代表15m趋势已经确认。"
                />
                <MetricItem
                  name="15m"
                  mode="stable"
                  summary="最近两根完成15m K线收盘价的变化百分比。"
                  reading="与5m同向时，短线方向通常比只有1m变化更稳定。"
                  caution="动能反映过去两根完成K线，不保证下一根继续同向。"
                />
              </div>
            </GuideSection>

            <GuideSection
              id="volume-funds"
              title="量价 / 资金"
              question="回答：走势有没有成交、主动资金和盘口挂单的配合？"
              icon={<BarChart3 className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="grid gap-3 md:grid-cols-2">
                <MetricItem
                  name="量比"
                  mode="stable"
                  summary="稳定15m周期成交量相对历史均量的倍数，缺失时只回退稳定30m。"
                  reading="大于1通常表示当前稳定周期成交比历史均量活跃。"
                  caution="放量不等于上涨，仍需结合价格方向。"
                />
                <MetricItem
                  name="量速"
                  mode="live"
                  summary="当前1m成交量投影，相对最近12根完成5m K线每分钟成交基准的倍数。"
                  reading="数值上升表示当前分钟成交正在加速。"
                  caution="分钟不足20秒、跨分钟或历史不足时显示--，不应用旧值补齐。"
                />
                <MetricItem
                  name="资金流入占比"
                  mode="stable"
                  summary="完整资金数据中，累计流入资金占流入与流出总和的比例。"
                  reading="高于50%表示累计流入占比较高，低于50%表示累计流出占比较高。"
                  caution="资金流入占比不是逐笔主动买入占比；数据不完整显示未确认，也不能单独作为买点。"
                />
                <MetricItem
                  name="五档"
                  mode="live"
                  summary="买一至买五与卖一至卖五挂单量的压力差百分比。"
                  reading="正值表示买盘挂单相对更强，负值表示卖盘挂单相对更强。"
                  caution="五档是当前挂单结构，撤单会使数值快速变化，不能视为成交承诺。"
                />
              </div>
            </GuideSection>

            <GuideSection
              id="breakout-risk"
              title="突破 / 风险"
              question="回答：离日内关键位置有多远，波动风险多大，稳定周期是否同向？"
              icon={<ShieldAlert className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="grid gap-3 md:grid-cols-2">
                <MetricItem
                  name="高"
                  mode="live"
                  summary="现价距离当日最高价的百分比。"
                  reading="越接近0%，越靠近日内高点，需要观察动能与量价是否同时增强。"
                  caution="接近日高不等于必然突破。"
                />
                <MetricItem
                  name="低"
                  mode="live"
                  summary="现价距离当日最低价的百分比。"
                  reading="越接近0%，越靠近日内低点，需要观察下行动能和卖盘是否增强。"
                  caution="接近日低不等于必然破位。"
                />
                <MetricItem
                  name="ATR14"
                  mode="stable"
                  summary="15m最近14个有效真实波幅，相对最新完成收盘价的百分比。"
                  reading="ATR14 越大表示短线波动风险越高，仓位和止损空间需要更谨慎。"
                  caution="ATR14只描述波动范围，不表示上涨或下跌。"
                />
                <MetricItem
                  name="振幅/ATR"
                  mode="live"
                  summary="当日日高低差除以最近完成15m K线计算出的绝对ATR14。"
                  reading="数值越高，表示当日已经走出的区间相对常态波动越大。"
                  caution="它不表示剩余波动空间一定耗尽，也不能单独判断方向。"
                />
                <MetricItem
                  name="周期确认"
                  mode="stable"
                  summary="周期确认只表示15m、30m两个稳定周期，显示0/2、1/2、2/2，并逐项标记。"
                  reading="2/2表示15m、30m都确认，1/2会明确指出仍待确认的周期。"
                  caution="确认数量表示一致性，最终方向仍看正式买卖信号。"
                />
                <MetricItem
                  name="数据时效"
                  mode="live"
                  summary="行情、盘口、1m K线和分析分别显示自己的数据年龄。"
                  reading="行、盘、K、析后的秒数越小，表示对应证据越接近当前时间。"
                  caution="源时间缺失显示--；延迟标记或超过90秒时自动弱化，但不会清除正式信号。"
                />
              </div>
            </GuideSection>

            <GuideSection
              id="sudden-anomaly"
              title="突发异动高亮"
              question="回答：哪个实时数字刚刚发生了足够大的跳变，需要优先检查？"
              icon={<Activity className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="rounded-lg border border-danger/30 bg-danger/5 p-3.5">
                <p className="text-sm font-semibold text-foreground">
                  红色“异动”只标记发生跳变的具体数字，高亮持续 10 秒。
                </p>
                <p className="mt-2 text-xs leading-5 text-muted">
                  它比较同一只股票相邻两次有效数据的绝对变化；达到或超过阈值后触发。
                  高亮期间再次发生达标跳变，会重新计时 10 秒。
                </p>
              </div>

              <div className="mt-3 grid gap-3 md:grid-cols-2">
                {[
                  ['涨跌幅 0.50 个百分点', '例如从 +1.20% 变为 +1.70%，或从 -0.30% 变为 -0.80%。'],
                  ['1m 涨速 0.40 个百分点', '观察当前分钟价格动能是否突然加速或转弱。'],
                  ['量速 1.00 倍', '观察当前分钟成交速度是否突然放大或收缩。'],
                  ['五档盘口 40 个百分点', '观察买卖五档压力差是否快速翻转或明显跳变。'],
                  ['距日高/日低 0.50 个百分点', '观察价格与日内关键高低点的距离是否快速变化。'],
                ].map(([threshold, explanation]) => (
                  <article key={threshold} className="rounded-lg border border-border bg-elevated/40 p-3.5">
                    <h3 className="text-sm font-semibold text-foreground">{threshold}</h3>
                    <p className="mt-2 text-xs leading-5 text-muted">{explanation}</p>
                  </article>
                ))}
              </div>

              <div className="mt-3 rounded-lg border border-warning/30 bg-warning/5 p-3.5 text-xs leading-5 text-muted">
                <p>
                  首次取得有效值只建立比较基线，不触发异动；数据缺失、无效或延迟时会清除基线和高亮，
                  恢复后的第一个有效值也只建立新基线。
                </p>
                <p className="mt-2 font-medium text-foreground">
                  突发异动仅作观察和排序提醒，不改变买卖信号，也不代表上涨或下跌方向。
                </p>
              </div>
            </GuideSection>

            <GuideSection
              id="scenarios"
              title="典型组合场景"
              question="组合示例用于理解指标之间的关系，不新增前端买卖结论。"
              icon={<BookOpenCheck className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div className="grid gap-3 md:grid-cols-2">
                {[
                  ['向上突破候选', '高接近0%，1m/5m/15m向上，量价增强，并且正式信号为买入。', '关注量价能否持续，不追逐单次盘口跳动。'],
                  ['向下破位风险', '低接近0%，短线动能向下，主动卖出或卖盘增强，正式信号为卖出/风险。', '先控制风险，不把接近日低直接等同于破位。'],
                  ['假突破警惕', '靠近日高或日低，但量价不配合，5m/15m分歧，确认仍为0/2。', '等待完成K线或后端正式信号确认。'],
                  ['继续观察', '距离高低点都较远、动能互相矛盾，或关键字段显示--/未确认。', '不为缺失数据补零，不强行给出方向。'],
                ].map(([title, condition, action]) => (
                  <article key={title} className="rounded-lg border border-border bg-elevated/40 p-3.5">
                    <h3 className="text-sm font-semibold text-foreground">{title}</h3>
                    <p className="mt-2 text-xs leading-5 text-muted">{condition}</p>
                    <p className="mt-2 text-xs leading-5 text-foreground/90">
                      <strong>阅读动作：</strong>{action}
                    </p>
                  </article>
                ))}
              </div>
            </GuideSection>

            <GuideSection
              id="quick-reference"
              title="指标速查表"
              question="快速确认更新类型、数值方向和最常见的误读。"
              icon={<Gauge className="h-4.5 w-4.5" aria-hidden="true" />}
            >
              <div
                data-testid="indicator-reference-scroll"
                className="max-w-full overflow-x-auto rounded-lg border border-border"
              >
                <table className="min-w-[900px] w-full border-collapse text-left text-xs">
                  <thead className="bg-elevated text-muted">
                    <tr>
                      {['指标', '更新', '越大通常表示', '越接近0通常表示', '缺失', '不能单独得出的结论'].map(label => (
                        <th key={label} scope="col" className="border-b border-border px-3 py-2.5 font-medium">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {REFERENCE_ROWS.map(row => (
                      <tr key={row[0]} className="border-b border-border/70 last:border-0">
                        <th scope="row" className="px-3 py-2.5 font-semibold text-foreground">{row[0]}</th>
                        <td className="px-3 py-2.5 text-muted">
                          <ModeBadge mode={row[1] === '实时' ? 'live' : 'stable'} />
                        </td>
                        {row.slice(2).map((value, index) => (
                          <td key={`${row[0]}-${index}`} className="px-3 py-2.5 leading-5 text-muted">{value}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-[11px] leading-5 text-muted">
                指标用于辅助理解后端正式信号的质量和执行风险，不构成收益保证或自动交易建议。
              </p>
            </GuideSection>
          </div>
        </div>
      </div>
    </main>
  )
}
