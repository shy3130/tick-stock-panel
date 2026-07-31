import { X } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Modal } from '@/components/Modal'

import {
  formatExchangeTradeDate,
  formatServerTimestamp,
} from './formatServerTimestamp'
import type { DowMonitorHalfHourAiSummary } from './types'
import { useDowMonitorAiDetail, useDowMonitorAiHistory } from './useDowMonitor'


function checkpoint(value: string | null): string {
  return formatServerTimestamp(value)?.slice(11) ?? '--'
}

export function DowMonitorAiAnalysisDialog({
  symbol,
  latest,
  onClose,
}: {
  symbol: string
  latest: DowMonitorHalfHourAiSummary
  onClose: () => void
}) {
  const tradeDate = formatExchangeTradeDate(latest.window_end, symbol)
  const history = useDowMonitorAiHistory(symbol, tradeDate, true)
  const [selectedId, setSelectedId] = useState(latest.analysis_id ?? '')
  useEffect(() => {
    if (!selectedId && history.data?.analyses[0]?.analysis_id) {
      setSelectedId(history.data.analyses[0].analysis_id)
    }
  }, [history.data, selectedId])
  const detail = useDowMonitorAiDetail(symbol, selectedId, Boolean(selectedId))
  const analysis = detail.data

  return (
    <Modal
      onClose={onClose}
      ariaLabel={`${symbol} 半小时综合分析`}
      panelClassName="flex max-h-[92vh] w-[96vw] max-w-4xl flex-col overflow-hidden rounded-card border border-border bg-surface shadow-xl"
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div>
          <h2 className="font-semibold">{symbol} 半小时综合分析</h2>
          <p className="text-xs text-muted">独立累计分析，不改变实时解读和买卖信号</p>
        </div>
        <button type="button" aria-label="关闭半小时分析" onClick={onClose}>
          <X className="h-5 w-5" />
        </button>
      </header>
      <div className="flex gap-2 overflow-x-auto border-b border-border px-4 py-2">
        {(history.data?.analyses ?? []).map(item => (
          <button
            key={item.analysis_id}
            type="button"
            onClick={() => setSelectedId(item.analysis_id ?? '')}
            className={`shrink-0 rounded-btn border px-2.5 py-1 text-xs ${
              selectedId === item.analysis_id
                ? 'border-accent text-accent'
                : 'border-border text-muted'
            }`}
          >
            {checkpoint(item.window_end)}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {detail.isLoading && <p className="text-sm text-muted">分析加载中…</p>}
        {detail.isError && <p className="text-sm text-danger">分析暂时不可用</p>}
        {analysis && (
          <div className="space-y-5 text-sm">
            <section>
              <p className="text-xs text-muted">
                截止 {formatServerTimestamp(analysis.data_cutoff)}（北京时间）
              </p>
              <h3 className="mt-1 text-lg font-semibold">{analysis.title}</h3>
              <p className="mt-2 leading-6 text-secondary">{analysis.conclusion}</p>
            </section>
            <section>
              <h4 className="font-medium">关键证据</h4>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {analysis.evidence.map(item => (
                  <div key={item.metric_key} className="rounded-card border border-border p-3">
                    <div className="flex justify-between gap-2">
                      <span>{item.label}</span><strong>{item.value}</strong>
                    </div>
                    <p className="mt-1 text-xs text-muted">{item.meaning}</p>
                  </div>
                ))}
              </div>
            </section>
            <section>
              <h4 className="font-medium">风险与不确定性</h4>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-secondary">
                {analysis.risks.map(item => <li key={item}>{item}</li>)}
              </ul>
            </section>
            {analysis.scenarios.length > 0 && (
              <section>
                <h4 className="font-medium">条件场景</h4>
                <div className="mt-2 space-y-2">
                  {analysis.scenarios.map((item, index) => (
                    <div key={`${item.condition}-${index}`} className="rounded-card bg-elevated p-3">
                      <strong>{item.condition}</strong>
                      <p className="mt-1 text-secondary">{item.implication}</p>
                      <p className="mt-1 text-xs text-muted">失效：{item.invalidates_when}</p>
                    </div>
                  ))}
                </div>
              </section>
            )}
            <section>
              <h4 className="font-medium">数据质量</h4>
              <p className="mt-1 text-xs text-muted">{analysis.data_quality.join('；')}</p>
            </section>
            <p className="border-t border-border pt-3 text-xs text-muted">
              本分析仅用于辅助识别盘中结构，不构成投资建议。
            </p>
          </div>
        )}
      </div>
    </Modal>
  )
}
