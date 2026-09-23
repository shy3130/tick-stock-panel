import { useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Loader2, Search, Table2, LineChart, Wind as WindIcon } from 'lucide-react'
import { api, type WindResponse } from '@/lib/api'

type Tab = 'quote' | 'kline' | 'search'

function nowISO() {
  return new Date().toISOString().slice(0, 10)
}

function monthsAgoISO(months: number) {
  const d = new Date()
  d.setMonth(d.getMonth() - months)
  return d.toISOString().slice(0, 10)
}

/** 把 Wind 返回的 {columns, rows} 拍平成对象数组。 */
function toRecords(data: WindResponse['data']): Record<string, unknown>[] {
  const inner = data?.data
  const cols = inner?.columns
  const rows = inner?.rows
  if (Array.isArray(cols) && Array.isArray(rows)) {
    return rows.map((row) => {
      const rec: Record<string, unknown> = {}
      cols.forEach((c, i) => {
        rec[String(c)] = (row as unknown[])[i]
      })
      return rec
    })
  }
  // 退路：data 本身就是数组
  if (Array.isArray(data)) return data as Record<string, unknown>[]
  return []
}

function WindTable({ data }: { data: WindResponse['data'] }) {
  const records = toRecords(data)
  if (records.length === 0) {
    return <div className="text-sm text-muted">暂无数据。请确认 Wind 代码 / 参数是否正确，或 Key 是否已配置。</div>
  }
  const columns = Object.keys(records[0])
  return (
    <div className="overflow-auto rounded-lg border border-border">
      <table className="min-w-full text-xs">
        <thead className="bg-elevated/60 text-secondary">
          <tr>
            {columns.map((c) => (
              <th key={c} className="whitespace-nowrap px-3 py-2 text-left font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {records.map((rec, i) => (
            <tr key={i} className="border-t border-border/60">
              {columns.map((c) => (
                <td key={c} className="whitespace-nowrap px-3 py-1.5 font-mono text-foreground/90">
                  {String(rec[c] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Wind() {
  const [tab, setTab] = useState<Tab>('quote')

  // quote
  const [windcode, setWindcode] = useState('600519.SH')
  const [indexes, setIndexes] = useState('')

  // kline
  const [kCode, setKCode] = useState('600519.SH')
  const [kBegin, setKBegin] = useState(monthsAgoISO(6))
  const [kEnd, setKEnd] = useState(nowISO())
  const [kPeriod, setKPeriod] = useState('1d')
  const [kAftype, setKAftype] = useState('0')

  // search
  const [question, setQuestion] = useState('沪深300成分股中近5日涨幅前10')

  const call = useMutation({
    mutationFn: async (req: {
      kind: Tab
      windcode?: string
      indexes?: string
      begin?: string
      end?: string
      period?: string
      aftype?: string
      question?: string
    }): Promise<WindResponse> => {
      if (req.kind === 'quote') return api.windQuote(req.windcode!, req.indexes || undefined)
      if (req.kind === 'kline')
        return api.windKline(req.windcode!, req.begin!, req.end!, req.period!, req.aftype!)
      return api.windSearch(req.question!)
    },
  })

  const status = useMemo(() => call.data, [call.data])
  const errMsg = call.data && !call.data.ok ? call.data.message ?? '请求失败' : null

  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-4 flex items-center gap-2">
        <WindIcon className="h-5 w-5 text-foreground/70" />
        <h1 className="text-lg font-semibold">Wind 行情</h1>
        <span className="text-xs text-muted">万得金融数据 · 单次透传，不缓存 / 不批量</span>
      </div>

      {/* 状态条 */}
      <div className="mb-4 flex items-center gap-2 text-xs">
        <button
          className="rounded-btn bg-elevated px-2 py-1 text-secondary hover:text-foreground"
          onClick={() => api.windStatus().then((s) => alert(JSON.stringify(s, null, 2)))}
        >
          探测 Wind CLI 状态
        </button>
        {call.isPending && (
          <span className="flex items-center gap-1 text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 请求中…
          </span>
        )}
      </div>

      {/* Tab 切换 */}
      <div className="mb-4 flex gap-1 border-b border-border">
        {([
          { key: 'quote', label: '个股行情', icon: Table2 },
          { key: 'kline', label: 'K 线', icon: LineChart },
          { key: 'search', label: '智能筛选', icon: Search },
        ] as const).map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex items-center gap-1.5 px-3 py-2 text-sm transition-colors ${
              tab === key
                ? 'border-b-2 border-accent text-foreground'
                : 'text-muted hover:text-foreground'
            }`}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}
      </div>

      {/* 错误提示 */}
      {errMsg && (
        <div className="mb-4 rounded-btn border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {errMsg}
        </div>
      )}

      {/* 行情 Tab */}
      {tab === 'quote' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-muted">
              Wind 代码
              <input
                className="rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm text-foreground"
                value={windcode}
                onChange={(e) => setWindcode(e.target.value)}
                placeholder="600519.SH"
              />
            </label>
            <label className="flex flex-1 flex-col gap-1 text-xs text-muted">
              指标（逗号分隔，可空）
              <input
                className="rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm text-foreground"
                value={indexes}
                onChange={(e) => setIndexes(e.target.value)}
                placeholder="最新价,涨跌幅,总市值"
              />
            </label>
            <button
              className="rounded-btn bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              disabled={call.isPending || !windcode}
              onClick={() => call.mutate({ kind: 'quote', windcode, indexes })}
            >
              查询
            </button>
          </div>
          {status?.ok && <WindTable data={status.data} />}
        </div>
      )}

      {/* K 线 Tab */}
      {tab === 'kline' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-muted">
              Wind 代码
              <input
                className="rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-sm text-foreground"
                value={kCode}
                onChange={(e) => setKCode(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              开始
              <input
                type="date"
                className="rounded-btn border border-border bg-base px-2 py-1.5 text-sm text-foreground"
                value={kBegin}
                onChange={(e) => setKBegin(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              结束
              <input
                type="date"
                className="rounded-btn border border-border bg-base px-2 py-1.5 text-sm text-foreground"
                value={kEnd}
                onChange={(e) => setKEnd(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              周期
              <select
                className="rounded-btn border border-border bg-base px-2 py-1.5 text-sm text-foreground"
                value={kPeriod}
                onChange={(e) => setKPeriod(e.target.value)}
              >
                {['1d', '1w', '1mo', '1q', '1y', '5min', '15min', '30min', '60min', '1min'].map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              复权
              <select
                className="rounded-btn border border-border bg-base px-2 py-1.5 text-sm text-foreground"
                value={kAftype}
                onChange={(e) => setKAftype(e.target.value)}
              >
                <option value="0">前复权</option>
                <option value="1">后复权</option>
                <option value="2">不复权</option>
              </select>
            </label>
            <button
              className="rounded-btn bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              disabled={call.isPending || !kCode}
              onClick={() => call.mutate({ kind: 'kline', windcode: kCode, begin: kBegin, end: kEnd, period: kPeriod, aftype: kAftype })}
            >
              查询
            </button>
          </div>
          {status?.ok && <WindTable data={status.data} />}
        </div>
      )}

      {/* 智能筛选 Tab */}
      {tab === 'search' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-1 flex-col gap-1 text-xs text-muted">
              自然语言条件
              <input
                className="rounded-btn border border-border bg-base px-2 py-1.5 text-sm text-foreground"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="沪深市场市值超500亿且连续5日上涨"
              />
            </label>
            <button
              className="rounded-btn bg-accent px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              disabled={call.isPending || !question}
              onClick={() => call.mutate({ kind: 'search', question })}
            >
              筛选
            </button>
          </div>
          {status?.ok && <WindTable data={status.data} />}
        </div>
      )}
    </div>
  )
}
