import { useRef, useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Database, X } from 'lucide-react'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { api } from '@/lib/api'
import type { EngineJobRecord, EngineJobRequest, EngineRepositoryDailyInput } from '@/lib/engineJobTypes'
import { cnToday, klineDateRange } from '@/lib/kline'
import { useDataStatus } from '@/lib/useSharedQueries'
import { Button, ErrorNotice, inputClass } from './ui'
import { newRequestKey } from './viewModel'

const EXCHANGES: Record<string, 'XSHG' | 'XSHE' | 'XBSE'> = {
  SH: 'XSHG',
  SZ: 'XSHE',
  BJ: 'XBSE',
}

function shiftDay(day: string, offset: number) {
  const value = new Date(day + 'T00:00:00Z')
  value.setUTCDate(value.getUTCDate() + offset)
  return value.toISOString().slice(0, 10)
}

function captureRequest(key: string, symbols: string[], start: string, end: string): EngineJobRequest {
  const bindings: EngineRepositoryDailyInput['symbols'] = symbols.map(symbol => ({
    symbol,
    instrument_id: 'cn-stock-' + symbol.toLowerCase(),
    venue_id: EXCHANGES[symbol.slice(-2)],
  }))
  const scope = {
    market_id: 'cn.equity',
    asset_class: 'equity',
    instrument_ids: bindings.map(item => item.instrument_id),
    start: start + 'T00:00:00+08:00',
    end: shiftDay(end, 1) + 'T00:00:00+08:00',
  } as const
  return {
    idempotency_key: key,
    nodes: [{
      node_id: 'prices',
      capture: {
        datasets: [
          {
            kind: 'tsp_repository_daily',
            layout: 'daily_raw_v1',
            expected_provider: 'local_archive',
            symbols: bindings,
            request: { ...scope, dataset_id: 'daily-raw', record_type: 'bar', timeframe: '1d', price_basis: 'raw' },
          },
          {
            kind: 'tsp_repository_factors',
            layout: 'event_factors_v1',
            representation: 'cumulative_forward',
            baseline_date: start,
            expected_provider: 'local_archive',
            symbols: bindings,
            request: { ...scope, dataset_id: 'factors', record_type: 'adjustment_factor' },
          },
        ],
      },
    }],
  }
}

export function SnapshotForm({ onCreated, onClose }: {
  onCreated: (job: EngineJobRecord) => void
  onClose: () => void
}) {
  const [selected, setSelected] = useState<Record<string, string>>({})
  const [dates, setDates] = useState<{ start?: string; end?: string }>({})
  const [selectionError, setSelectionError] = useState('')
  const request = useRef<{ scope: string; key: string } | null>(null)
  const daily = useDataStatus().data?.daily
  const yesterday = shiftDay(cnToday(), -1)
  const maxEnd = daily?.latest_date && daily.latest_date < yesterday ? daily.latest_date : yesterday
  const defaultStart = klineDateRange(6, maxEnd).start
  const start = dates.start ?? (daily?.earliest_date && daily.earliest_date > defaultStart ? daily.earliest_date : defaultStart)
  const end = dates.end ?? maxEnd
  const submit = useMutation({
    mutationFn: async (body: EngineJobRequest) => {
      const preview = await api.previewEngineJob(body)
      return api.submitEngineJob(preview.plan.request, preview.plan_id)
    },
  })

  function generate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submit.isPending) return
    const symbols = Object.keys(selected).sort()
    if (!symbols.length) {
      setSelectionError('请搜索并添加至少一只股票。')
      return
    }
    const scope = JSON.stringify({ symbols, start, end })
    if (request.current?.scope !== scope) request.current = { scope, key: newRequestKey() }
    submit.mutate(captureRequest(request.current.key, symbols, start, end), { onSuccess: onCreated })
  }

  return (
    <form onSubmit={generate} className="w-full max-w-3xl p-3 sm:px-4">
      <header className="mb-3 border-b border-border pb-2.5">
        <h2 className="text-[16px] font-semibold text-foreground">生成日K快照</h2>
        <p className="mt-0.5 text-sm leading-5 text-secondary">把所选 A 股的本地日K和复权因子保存为固定版本，供策略回测使用。</p>
      </header>
      <fieldset disabled={submit.isPending} className="min-w-0 space-y-3">
        <div>
          <label htmlFor="snapshot-symbol" className="mb-1.5 block text-sm font-medium">回测标的</label>
          <StockFinancialSearch inputId="snapshot-symbol" assetTypes="stock" className="max-w-none [&_input]:h-8 [&_input]:rounded-btn [@media(pointer:coarse)]:[&_input]:h-9" onSelect={(symbol, name) => {
            if (!/^[0-9]{6}\.(SH|SZ|BJ)$/.test(symbol)) return
            setSelected(current => ({ ...current, [symbol]: name }))
            setSelectionError('')
            submit.reset()
          }} />
          {selectionError && <p role="alert" className="mt-2 text-sm text-danger">{selectionError}</p>}
          {Object.keys(selected).length > 0 && (
            <ul aria-label="已选标的" className="mt-1.5 flex flex-wrap gap-1.5">
              {Object.entries(selected).map(([symbol, name]) => (
                <li key={symbol}>
                  <Button compact
                    aria-label={`移除 ${name || symbol}`}
                    onClick={() => {
                      setSelected(({ [symbol]: _, ...remaining }) => remaining)
                      submit.reset()
                    }}
                  >
                    {name && <span>{name}</span>}
                    <span className="font-mono text-secondary">{symbol}</span>
                    <X aria-hidden="true" className="ml-1 size-3.5 text-secondary" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <div className="grid gap-2.5 min-[360px]:grid-cols-2">
            <label className="block text-sm font-medium">
              <span className="mb-1.5 block">开始日期</span>
              <input type="date" required value={start} min={daily?.earliest_date ?? undefined} max={end || maxEnd}
                className={inputClass} onChange={event => { setDates(value => ({ ...value, start: event.target.value })); submit.reset() }} />
            </label>
            <label className="block text-sm font-medium">
              <span className="mb-1.5 block">结束日期</span>
              <input type="date" required value={end} min={start || daily?.earliest_date || undefined} max={maxEnd}
                className={inputClass} onChange={event => { setDates(value => ({ ...value, end: event.target.value })); submit.reset() }} />
            </label>
          </div>
          <p className="mt-1.5 text-xs leading-5 text-secondary">开始和结束日期均计入快照。策略需要预热时，可把开始日期选早一些。</p>
        </div>
      </fieldset>

      {submit.error && <div className="mt-3"><ErrorNotice error={submit.error} /></div>}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2.5">
        <p className="text-xs text-secondary" role="status">
          {submit.isPending ? '正在检查本地数据并提交任务…' : `已选 ${Object.keys(selected).length} 只股票`}
        </p>
        <div className="ml-auto flex gap-2">
          <Button variant="quiet" disabled={submit.isPending} onClick={onClose}>取消</Button>
          <Button variant="primary" type="submit" disabled={submit.isPending} aria-busy={submit.isPending}>
            <Database aria-hidden="true" className="size-3.5" />
            {submit.isPending ? '正在提交…' : '生成快照'}
          </Button>
        </div>
      </div>
    </form>
  )
}
