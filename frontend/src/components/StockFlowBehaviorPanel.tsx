import { useState } from 'react'
import { ChevronDown, Info } from 'lucide-react'
import { StockTransScatter } from '@/components/StockTransScatter'
import { StockMoneyflowPanel } from '@/components/StockMoneyflowPanel'

/**
 * 资金行为视图（个股详情底部折叠区）。
 *
 * 形态参考「风险承担者-资金行为分析系统」系列（Obsidian
 * Note/fm/filter/）：01 逐笔散点 + 02 模块一大单净额面板。
 * 方向维度（主动买/卖四维、换手度）因逐笔 direction 字段语义
 * 未统一而暂缓，展开区头部显式说明，不静默假装完整。
 *
 * 折叠时不挂载子图（ECharts init 需要容器有实际尺寸），
 * 展开时才挂载并按容器宽度自适应。
 */

interface Props {
  symbol: string
  date: string | null
}

export function StockFlowBehaviorPanel({ symbol, date }: Props) {
  const [open, setOpen] = useState(false)

  if (!symbol || !date) return null

  return (
    <div className="mt-3 border border-border rounded-panel overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-elevated/40 transition-colors"
      >
        <span className="flex items-center gap-2 text-xs font-medium text-secondary">
          <span className="font-mono text-muted">资金行为</span>
          <span className="text-[10px] text-muted/70 hidden sm:inline">
            逐笔散点（金额维度） · 近6日大单净额
          </span>
        </span>
        <ChevronDown
          className={`h-3.5 w-3.5 text-muted transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div className="border-t border-border">
          <div className="flex items-start gap-1.5 px-3 py-1.5 bg-elevated/20">
            <Info className="h-3 w-3 text-muted/70 mt-0.5 shrink-0" />
            <span className="text-[10px] leading-relaxed text-muted/80">
              逐笔买卖方向维度暂未开放：数据源 direction 编码在下游三个项目语义
              不一致且无权威定义，待上游统一后再启用；当前散点按单笔金额分档。
            </span>
          </div>
          <div className="flex flex-col lg:flex-row gap-3 p-2">
            <div className="flex-1 min-w-0">
              <StockTransScatter symbol={symbol} date={date} height={280} />
            </div>
            <div className="flex-1 min-w-0 lg:border-l lg:border-border lg:pl-3">
              <StockMoneyflowPanel symbol={symbol} date={date} height={280} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
