import { motion } from 'framer-motion'
import { Link } from 'react-router-dom'
import {
  Activity,
  ArrowRight,
  BarChart3,
  BellRing,
  Database,
  FileText,
  GitBranch,
  LineChart,
  Radar,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Target,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import dashboardPreview from '../../../screenshots/dashboard.png'
import { BRAND_NAME } from '@/lib/brand'

const mechanisms: Array<{
  title: string
  kicker: string
  desc: string
  icon: LucideIcon
}> = [
  {
    title: '行情与数据管线',
    kicker: 'Market Base',
    desc: '统一整理日线、分钟、指数、自选、盘口与扩展字段，为后续分析提供稳定底座。',
    icon: Database,
  },
  {
    title: '策略选股与信号',
    kicker: 'Signal Engine',
    desc: '用内置策略、自定义信号和全市场扫描，把交易想法转成可重复执行的条件。',
    icon: ScanSearch,
  },
  {
    title: '回测验证与风控',
    kicker: 'Backtest Check',
    desc: '通过历史回测、成本约束、止损参数和组合观察，先验证，再进入监控。',
    icon: ShieldCheck,
  },
  {
    title: '盘中监控与复盘',
    kicker: 'Live Monitor',
    desc: '把策略、价格、异动和自选信号放进同一套盘中观察与盘后复盘流程。',
    icon: BellRing,
  },
  {
    title: '图谱关系与扩展',
    kicker: 'Graph Layer',
    desc: '把个股、概念、行业和扩展数据连接起来，作为增强分析层观察传导关系。',
    icon: GitBranch,
  },
]

const capabilities: Array<{
  title: string
  desc: string
  icon: LucideIcon
}> = [
  {
    title: '市场看板',
    desc: '指数、涨跌分布、情绪雷达、板块热度和异动流集中呈现。',
    icon: BarChart3,
  },
  {
    title: '策略选股',
    desc: '内置策略与自定义信号并行，适合快速扫描和持续调参。',
    icon: Target,
  },
  {
    title: '回测验证',
    desc: '在投入监控前，用净值、回撤、胜率和交易明细校验策略边界。',
    icon: LineChart,
  },
  {
    title: '监控中心',
    desc: '盘中触发记录、实时提醒和规则管理组成一个连续观察面。',
    icon: Radar,
  },
  {
    title: '数据扩展',
    desc: '接入自定义字段，把概念、行业和外部维度放到同一张工作台。',
    icon: Database,
  },
  {
    title: 'AI 分析',
    desc: '辅助生成个股、财务和盘后复盘解释，保留人工判断的最终位置。',
    icon: Sparkles,
  },
]

const research = [
  {
    title: '个股关系图谱',
    state: '分析链路中',
    desc: '围绕个股、概念、行业与上下游关系构建局部网络，帮助观察单一线索如何传导到关联标的。',
  },
  {
    title: '消息权重研究',
    state: '下一阶段',
    desc: '逐步把公告、新闻、题材和舆情转译为可验证的量化权重，而不是给出不可追溯的结论。',
  },
]

function FadeIn({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  return (
    <motion.div
      initial={false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  )
}

export function Landing() {
  return (
    <div className="min-h-screen overflow-x-hidden bg-[#151410] text-[#f4f1e8] selection:bg-[#b9a46a]/30 selection:text-white">
      <header className="fixed inset-x-0 top-0 z-30 border-b border-[#b9a46a]/10 bg-[#151410]/78 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5 md:px-8">
          <a href="#top" className="font-mono text-sm font-semibold tracking-[0.28em] text-[#f4f1e8]">
            {BRAND_NAME}
          </a>
          <nav className="hidden items-center gap-9 font-mono text-[11px] tracking-[0.2em] text-[#b9b2a0]/72 md:flex">
            <a href="#system" className="transition-colors hover:text-[#b9a46a]">系统</a>
            <a href="#workbench" className="transition-colors hover:text-[#b9a46a]">能力</a>
            <a href="#research" className="transition-colors hover:text-[#b9a46a]">研究</a>
          </nav>
          <Link
            to="/login"
            className="inline-flex h-9 items-center justify-center rounded-md border border-[#b9a46a]/35 px-4 font-mono text-[11px] tracking-[0.16em] text-[#f4f1e8] transition-colors hover:border-[#b9a46a] hover:bg-[#b9a46a]/10"
          >
            工作台
          </Link>
        </div>
      </header>

      <main id="top">
        <section className="relative min-h-[88dvh] overflow-hidden pt-16">
          <div className="absolute inset-0 bg-[linear-gradient(115deg,#151410_0%,#151410_34%,rgba(21,20,16,0.86)_54%,rgba(21,20,16,0.44)_100%)]" />
          <div className="absolute inset-0 opacity-[0.08] [background-image:linear-gradient(rgba(185,164,106,0.5)_1px,transparent_1px),linear-gradient(90deg,rgba(185,164,106,0.42)_1px,transparent_1px)] [background-size:64px_64px]" />
          <div className="pointer-events-none absolute inset-y-0 right-0 hidden w-[78%] overflow-hidden md:block">
            <img
              src={dashboardPreview}
              alt={`${BRAND_NAME} 工作台行情看板预览`}
              className="ml-auto h-full w-[145%] max-w-none object-cover object-right-top opacity-[0.26] saturate-[0.82]"
            />
            <div className="absolute inset-0 bg-[linear-gradient(90deg,#151410_0%,rgba(21,20,16,0.82)_30%,rgba(21,20,16,0.18)_100%)]" />
          </div>
          <div className="relative mx-auto flex min-h-[calc(88dvh-4rem)] max-w-6xl items-center px-5 py-16 md:px-8">
            <motion.div
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.65, ease: [0.16, 1, 0.3, 1] }}
              className="max-w-3xl"
            >
              <div className="font-mono text-[11px] tracking-[0.34em] text-[#b9a46a]">
                SERVER DEPLOYED QUANT WORKBENCH
              </div>
              <h1 className="mt-7 text-5xl font-semibold leading-[0.96] tracking-normal text-white md:text-7xl">
                {BRAND_NAME}
              </h1>
              <div className="mt-5 text-lg font-medium tracking-[0.28em] text-[#d8cfb7] md:text-xl">
                服务器部署 A 股量化工作台
              </div>
              <p className="mt-8 max-w-2xl text-[1rem] font-light leading-9 tracking-[0.12em] text-[#e4dcc8] drop-shadow-[0_1px_12px_rgba(0,0,0,0.75)] md:text-lg">
                在行情表象之外，连接数据、策略、回测与监控，构建自己的量化观测系统。
              </p>
              <div className="mt-10 flex flex-col gap-3 sm:flex-row">
                <Link
                  to="/login"
                  className="inline-flex h-12 items-center justify-center gap-2 rounded-md bg-[#b9a46a] px-6 text-sm font-semibold tracking-[0.12em] text-[#17140d] transition-transform hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-[#d8c47e]/70"
                >
                  进入工作台
                  <ArrowRight className="h-4 w-4" />
                </Link>
                <a
                  href="#system"
                  className="inline-flex h-12 items-center justify-center rounded-md border border-[#b9a46a]/28 px-6 text-sm font-medium tracking-[0.12em] text-[#efe9d9] transition-colors hover:border-[#b9a46a] hover:bg-[#b9a46a]/8 focus:outline-none focus:ring-2 focus:ring-[#d8c47e]/50"
                >
                  查看系统机制
                </a>
              </div>
            </motion.div>
          </div>
        </section>

        <section id="system" className="border-y border-[#b9a46a]/10 bg-[#191813]">
          <div className="mx-auto max-w-6xl px-5 py-16 md:px-8 md:py-20">
            <FadeIn>
              <div className="max-w-2xl">
                <div className="font-mono text-[11px] tracking-[0.28em] text-[#b9a46a]/80">SYSTEM MECHANISM</div>
                <h2 className="mt-4 text-2xl font-semibold tracking-normal text-white md:text-3xl">
                  从数据进入，到信号被验证
                </h2>
                <p className="mt-4 text-sm leading-7 tracking-[0.08em] text-[#aaa392]">
                  {BRAND_NAME} 把看盘、选股、验证、监控和复盘压进一条连续工作流。每一步都能回到数据，不靠一句结论结束判断。
                </p>
              </div>
            </FadeIn>

            <div className="mt-12 grid gap-3 lg:grid-cols-5">
              {mechanisms.map((item, index) => (
                <FadeIn key={item.title} delay={index * 0.04}>
                  <div className="group h-full border border-[#b9a46a]/12 bg-[#222019]/62 p-5 transition-colors hover:border-[#b9a46a]/38 hover:bg-[#262319]/78">
                    <div className="flex items-center justify-between gap-3">
                      <item.icon className="h-4 w-4 text-[#b9a46a]" />
                      <span className="font-mono text-[10px] text-[#7d7768]">{String(index + 1).padStart(2, '0')}</span>
                    </div>
                    <div className="mt-7 font-mono text-[10px] tracking-[0.18em] text-[#b9a46a]/72">{item.kicker}</div>
                    <h3 className="mt-3 text-[1rem] font-medium text-[#f5f1e7]">{item.title}</h3>
                    <p className="mt-4 text-xs leading-6 tracking-[0.06em] text-[#aaa392]">{item.desc}</p>
                  </div>
                </FadeIn>
              ))}
            </div>
          </div>
        </section>

        <section id="workbench" className="bg-[#151410]">
          <div className="mx-auto max-w-6xl px-5 py-16 md:px-8 md:py-24">
            <FadeIn>
              <div className="grid gap-6 md:grid-cols-[0.95fr_1.3fr] md:items-end">
                <div>
                  <div className="font-mono text-[11px] tracking-[0.28em] text-[#b9a46a]/80">WORKBENCH</div>
                  <h2 className="mt-4 text-2xl font-semibold tracking-normal text-white md:text-4xl">
                    不是单点工具，是完整工作台
                  </h2>
                </div>
                <p className="max-w-2xl text-sm leading-7 tracking-[0.08em] text-[#aaa392]">
                  你可以只用它看市场，也可以把策略、规则、扩展数据和复盘流程逐步接进来。{BRAND_NAME} 的重点是把研究动作沉淀成可重复的流程。
                </p>
              </div>
            </FadeIn>

            <div className="mt-12 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {capabilities.map((item, index) => (
                <FadeIn key={item.title} delay={index * 0.035}>
                  <div className="min-h-44 border border-[#2c2a22] bg-[#1c1b16] p-6 transition-colors hover:border-[#b9a46a]/36">
                    <div className="flex h-9 w-9 items-center justify-center rounded-md bg-[#b9a46a]/10 text-[#b9a46a]">
                      <item.icon className="h-4 w-4" />
                    </div>
                    <h3 className="mt-6 text-lg font-medium text-[#f5f1e7]">{item.title}</h3>
                    <p className="mt-3 text-sm leading-7 tracking-[0.04em] text-[#aaa392]">{item.desc}</p>
                  </div>
                </FadeIn>
              ))}
            </div>
          </div>
        </section>

        <section id="research" className="border-y border-[#b9a46a]/10 bg-[#1a1813]">
          <div className="mx-auto max-w-6xl px-5 py-16 md:px-8 md:py-20">
            <FadeIn>
              <div className="max-w-3xl">
                <div className="font-mono text-[11px] tracking-[0.28em] text-[#b9a46a]/80">RESEARCH LAYER</div>
                <h2 className="mt-4 text-2xl font-semibold tracking-normal text-white md:text-3xl">
                  关系图谱进入分析链路，消息权重仍在研究
                </h2>
                <p className="mt-4 text-sm leading-7 tracking-[0.08em] text-[#aaa392]">
                  图谱是工作台中的增强分析层。消息面转权重是下一阶段方向，目标是把更多线索放进可验证框架，而不是替代人的判断。
                </p>
              </div>
            </FadeIn>

            <div className="mt-10 grid gap-4 md:grid-cols-2">
              {research.map((item) => (
                <FadeIn key={item.title}>
                  <article className="border border-[#b9a46a]/14 bg-[#211f18] p-7">
                    <div className="flex items-center justify-between gap-4">
                      <h3 className="text-xl font-medium text-[#f5f1e7]">{item.title}</h3>
                      <span className="shrink-0 rounded-full border border-[#b9a46a]/28 px-3 py-1 font-mono text-[10px] tracking-[0.12em] text-[#b9a46a]">
                        {item.state}
                      </span>
                    </div>
                    <p className="mt-5 text-sm leading-7 tracking-[0.06em] text-[#aaa392]">{item.desc}</p>
                  </article>
                </FadeIn>
              ))}
            </div>
          </div>
        </section>

        <section className="bg-[#151410]">
          <div className="mx-auto grid max-w-6xl gap-8 px-5 py-16 md:grid-cols-[1.1fr_0.9fr] md:px-8 md:py-20">
            <FadeIn>
              <div>
                <Activity className="h-5 w-5 text-[#b9a46a]" />
                <h2 className="mt-5 text-2xl font-semibold tracking-normal text-white md:text-3xl">
                  部署在服务器上访问
                </h2>
                <p className="mt-5 max-w-2xl text-sm leading-8 tracking-[0.08em] text-[#aaa392]">
                  {BRAND_NAME} 适合部署到云服务器或私有服务器，通过浏览器访问工作台。配置、规则、回测记录和扩展数据由服务端统一保存，便于长期维护和多人协作。
                </p>
              </div>
            </FadeIn>
            <FadeIn delay={0.06}>
              <div className="border border-[#b9a46a]/16 bg-[#211f18] p-7">
                <FileText className="h-5 w-5 text-[#b9a46a]" />
                <h3 className="mt-5 text-lg font-medium text-[#f5f1e7]">边界说明</h3>
                <p className="mt-4 text-sm leading-7 tracking-[0.06em] text-[#aaa392]">
                  本工具只用于学习、研究和流程化观察，不提供投资建议，不承诺任何收益。所有判断都应回到你的策略、数据和风险约束。
                </p>
                <Link
                  to="/login"
                  className="mt-7 inline-flex h-11 items-center justify-center gap-2 rounded-md bg-[#b9a46a] px-5 text-sm font-semibold tracking-[0.1em] text-[#17140d] transition-transform hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-[#d8c47e]/70"
                >
                  进入工作台
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </div>
            </FadeIn>
          </div>
        </section>
      </main>

      <footer className="border-t border-[#b9a46a]/10 bg-[#11100d]">
        <div className="mx-auto flex max-w-6xl flex-col gap-3 px-5 py-8 font-mono text-[10px] tracking-[0.18em] text-[#777061] md:flex-row md:items-center md:justify-between md:px-8">
          <div>© 2026 SYCEE.</div>
          <div>SERVER DEPLOYED QUANT WORKBENCH</div>
        </div>
      </footer>
    </div>
  )
}
