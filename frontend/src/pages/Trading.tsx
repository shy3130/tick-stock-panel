import { Cable } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { getNavIconMeta } from '@/lib/navRegistry'
import { EmptyState } from '@/components/EmptyState'

// 后续实现计划(本轮为占位):
//
// 一、信号 → 交易 的桥接
//   监控通知产生的买卖信号(StrategyAlert),通过可插拔的输出通道分发到
//   支持外部信号的交易软件。核心是在 alert_handler 层做多通道分发。
//
// 二、支持的交易软件(按接入难度)
//   1. QMT / miniQMT(迅投)—— 个人 A 股实盘首选。
//      XtQuant 的 xttrader.order_stock() 下单,信号来源不限(文件/HTTP)。
//   2. 掘金量化(MyQuant)—— 本地终端 + Python SDK,事件驱动接收信号。
//   3. Ptrade(恒生)—— 内置策略引擎,外部信号经 API/文件喂入。
//   4. vnpy(VeighNa)—— 开源框架,自写策略模块接收信号再调 Gateway 下单。
//
// 三、信号输出通道(可插拔)
//   alert_handler 分发:
//     ├─ SSE → 前端通知(已有)
//     ├─ 本地文件(JSON/CSV) → QMT 脚本轮询读取  ← 最简单,优先做
//     ├─ Webhook POST → 外部交易脚本
//     └─ 直连 xttrader(需本机装 QMT)
//
// 四、信号 → 交易指令 的字段补全
//   现有 StrategyAlert(symbol/type/strategy_id/price)是「信号层」,
//   下单还需补:volume(数量,A股100的倍数)、price_type(市价/限价)、account。
const PLAN: { title: string; desc: string }[] = [
  {
    title: 'QMT / miniQMT',
    desc: '个人 A 股实盘首选。XtQuant 的 xttrader 下单,信号经文件或 HTTP 喂入即可。国内个人量化实盘事实标准。',
  },
  {
    title: '掘金量化 (MyQuant)',
    desc: '本地终端 + Python SDK,事件驱动接收外部信号下单,本土化程度高。',
  },
  {
    title: 'Ptrade (恒生)',
    desc: '内置 Python 策略引擎,外部信号经 API/文件喂入,灵活性低于 QMT。',
  },
  {
    title: 'vnpy (VeighNa)',
    desc: '开源交易框架,Gateway 丰富(期货/股票/加密货币),需自建执行端,搭建成本较高。',
  },
  {
    title: '信号输出通道',
    desc: 'alert_handler 多通道分发:本地文件(最简,优先)、Webhook POST、直连 xttrader。与具体交易软件解耦。',
  },
]

export function Trading() {
  return (
    <div className="flex flex-col h-full">
      <PageHeader title="交易" {...getNavIconMeta('/trading')} subtitle="信号自动下单桥接 · 开发中" />

      <div className="flex-1 overflow-auto px-5 py-6">
        <div className="max-w-3xl mx-auto">
          <EmptyState
            icon={Cable}
            title="交易桥接开发中"
            hint="本页面将把监控产生的买卖信号,自动推送给支持外部信号的交易软件(QMT/掘金/Ptrade 等)执行下单。当前为占位页面,下方为后续实现规划。"
          />

          <section className="mt-6 rounded-card border border-border bg-surface p-5">
            <h3 className="text-sm font-semibold text-foreground">后续实现规划</h3>
            <ul className="mt-3 space-y-3">
              {PLAN.map((item) => (
                <li key={item.title} className="flex gap-3">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
                  <div>
                    <p className="text-sm font-medium text-foreground">{item.title}</p>
                    <p className="mt-0.5 text-xs leading-relaxed text-secondary">{item.desc}</p>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </div>
  )
}
