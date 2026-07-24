import { Check, Copy } from 'lucide-react'
import type { ExtDataConfig } from '@/lib/api'

export function ExtDataApiPanel({ config, copied, setCopied }: {
  config: ExtDataConfig
  copied: boolean
  setCopied: (v: boolean) => void
}) {
  const endpoint = `POST /api/ext-data/${config.id}/ingest`

  const exampleRow: Record<string, unknown> = { symbol: '000001.SZ' }
  for (const f of config.fields) {
    if (f.name === 'symbol' || f.name === 'code') continue
    exampleRow[f.name] = f.dtype === 'int' ? 100 : f.dtype === 'float' ? 1.5 : f.dtype === 'bool' ? true : '示例'
  }
  if (config.fields.some(f => f.name === 'code')) {
    exampleRow['code'] = '000001'
  }
  const exampleBody = {
    date: config.mode === 'timeseries' ? '2025-01-15' : undefined,
    rows: [exampleRow],
  }
  const exampleJson = JSON.stringify(exampleBody, null, 2)
  const curlCmd = `curl -X POST http://localhost:${window.location.port || '3018'}/api/ext-data/${config.id}/ingest \\
  -H 'Content-Type: application/json' \\
  -d '${JSON.stringify(exampleBody)}'`

  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <div className="space-y-2.5">
      <div>
        <div className="text-[10px] text-muted mb-1">接口端点</div>
        <div className="flex items-center gap-1.5 bg-elevated rounded-md px-2.5 py-1.5">
          <code className="text-[11px] font-mono text-accent flex-1 select-all">{endpoint}</code>
          <button onClick={() => handleCopy(endpoint)} className="text-muted hover:text-secondary transition-colors">
            {copied ? <Check className="h-3 w-3 text-accent" /> : <Copy className="h-3 w-3" />}
          </button>
        </div>
      </div>

      <div>
        <div className="text-[10px] text-muted mb-1">请求体示例</div>
        <pre className="bg-elevated rounded-md px-2.5 py-2 text-[10px] font-mono text-secondary overflow-x-auto whitespace-pre leading-relaxed">
          {exampleJson}
        </pre>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-[10px] text-muted">cURL 示例</span>
          <button
            onClick={() => handleCopy(curlCmd)}
            className="text-[10px] text-accent hover:text-accent/80 flex items-center gap-0.5 transition-colors"
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            复制
          </button>
        </div>
        <pre className="bg-elevated rounded-md px-2.5 py-2 text-[10px] font-mono text-secondary overflow-x-auto whitespace-pre-wrap leading-relaxed break-all">
          {curlCmd}
        </pre>
      </div>

      <div className="text-[10px] text-muted leading-relaxed">
        {config.mode === 'snapshot' ? (
          <><span className="text-secondary">date</span> 可选，默认当天；每次写入覆盖同日期数据</>
        ) : (
          <><span className="text-secondary">date</span> 必填，指定交易日；按日期分区存储</>
        )}
      </div>
    </div>
  )
}
