import { useState } from 'react'
import { Loader2, Search, Check, Clock, Zap, Settings2, AlertCircle, CheckCircle2, Calendar } from 'lucide-react'
import { api, type ExtDataConfig } from '@/lib/api'
import { toast } from '@/components/Toast'

export function ExtDataPullPanel({ config, onSaved }: {
  config: ExtDataConfig
  onSaved: () => void
}) {
  const pull = config.pull
  const [url, setUrl] = useState(pull?.url ?? '')
  const [method, setMethod] = useState(pull?.method ?? 'GET')
  const [headerStr, setHeaderStr] = useState(
    pull?.headers ? JSON.stringify(pull.headers, null, 2) : ''
  )
  const [body, setBody] = useState(pull?.body ?? '')
  const [responsePath, setResponsePath] = useState(pull?.response_path ?? '')
  const [fieldMapStr, setFieldMapStr] = useState(
    pull?.field_map ? JSON.stringify(pull.field_map, null, 2) : ''
  )
  const [schedule, setSchedule] = useState(pull?.schedule_minutes ?? 1440)
  const [enabled, setEnabled] = useState(pull?.enabled ?? false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<{ rows: number; date: string } | null>(null)
  const [testResult, setTestResult] = useState<{ total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean } | null>(null)
  const [error, setError] = useState('')

  // 解析 JSON 输入, 失败时设置 error 并返回 null
  const parseJson = (str: string, label: string): Record<string, string> | undefined | null => {
    if (!str.trim()) return undefined
    try { return JSON.parse(str) }
    catch { setError(`${label} 不是有效 JSON`); return null }
  }

  // 构建保存 payload (复用当前编辑态), enabledOverride 用于开关自动保存
  const buildPayload = (enabledOverride?: boolean) => {
    const headers = parseJson(headerStr, 'Headers')
    if (headers === null) return null
    const field_map = parseJson(fieldMapStr, '字段映射')
    if (field_map === null) return null
    return {
      url, method, headers, body: body || undefined,
      response_path: responsePath, field_map,
      schedule_minutes: schedule, enabled: enabledOverride ?? enabled,
    }
  }

  const handleSave = (silent = false) => {
    const payload = buildPayload()
    if (!payload) return
    setSaving(true); setError('')
    api.extDataPullConfig(config.id, payload)
      .then(() => {
        onSaved()
        if (!silent) toast('配置已保存', 'success')
      })
      .catch(e => setError(e.message || '保存失败'))
      .finally(() => setSaving(false))
  }

  const handleTest = () => {
    setTesting(true); setError(''); setTestResult(null)
    const payload = buildPayload()
    if (!payload) { setTesting(false); return }
    api.extDataPullConfig(config.id, payload)
      .then(() => api.extDataPullTest(config.id))
      .then(r => { setTestResult(r); onSaved() })
      .catch(e => setError(e.message || '测试失败'))
      .finally(() => setTesting(false))
  }

  const handleRun = () => {
    setRunning(true); setError(''); setRunResult(null)
    api.extDataPullRun(config.id)
      .then(r => {
        setRunResult({ rows: r.rows, date: r.date })
        onSaved()
        toast(`拉取成功 · ${r.rows} 行`, 'success')
      })
      .catch(e => setError(e.message || '执行失败'))
      .finally(() => setRunning(false))
  }

  // 开关 toggle: 自动保存全量配置 (切换 enabled), 后端 refresh 后立即首次拉取
  const [toggling, setToggling] = useState(false)
  const handleToggle = (next: boolean) => {
    if (toggling) return
    if (next && !url.trim()) {
      toast('请先填写拉取 URL', 'error')
      return
    }
    const payload = buildPayload(next)
    if (!payload) return
    setToggling(true); setError(''); setEnabled(next)
    api.extDataPullConfig(config.id, payload)
      .then(() => {
        onSaved()
        toast(next ? '定时拉取已启用 · 立即执行首次拉取' : '定时拉取已关闭', 'success')
      })
      .catch(e => {
        setEnabled(!next)  // 回滚
        setError(e.message || '切换失败')
      })
      .finally(() => setToggling(false))
  }

  // 格式化时间显示
  const fmtTime = (iso: string | null | undefined) => {
    if (!iso) return null
    const d = new Date(iso)
    if (isNaN(d.getTime())) return null
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mi = String(d.getMinutes()).padStart(2, '0')
    return `${mm}-${dd} ${hh}:${mi}`
  }

  return (
    <div className="space-y-3">
      {/* ===== 分区 ①: 请求配置 ===== */}
      <div className="space-y-2">
        <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
          <Settings2 className="h-3 w-3 text-muted" />
          <span>请求配置</span>
        </div>

        <div className="flex gap-1.5">
          <select
            value={method} onChange={e => setMethod(e.target.value)}
            className="shrink-0 rounded-btn border border-border bg-elevated px-2 py-1.5 text-[11px] text-foreground"
          >
            <option value="GET">GET</option>
            <option value="POST">POST</option>
          </select>
          <input
            value={url} onChange={e => setUrl(e.target.value)}
            placeholder="https://api.example.com/data"
            className="flex-1 min-w-0 rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[11px] font-mono text-foreground placeholder:text-muted/50"
          />
        </div>

        <div>
          <div className="text-[10px] text-muted mb-1">Headers (JSON，可选)</div>
          <textarea
            value={headerStr} onChange={e => setHeaderStr(e.target.value)}
            placeholder='{"Authorization": "Bearer xxx"}'
            rows={2}
            className="w-full rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none"
          />
        </div>

        {method === 'POST' && (
          <div>
            <div className="text-[10px] text-muted mb-1">请求体 (JSON，可选)</div>
            <textarea
              value={body} onChange={e => setBody(e.target.value)}
              placeholder='{"page": 1}'
              rows={2}
              className="w-full rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none"
            />
          </div>
        )}

        <div className="grid grid-cols-2 gap-2">
          <div>
            <div className="text-[10px] text-muted mb-1">响应数据路径</div>
            <input
              value={responsePath} onChange={e => setResponsePath(e.target.value)}
              placeholder="data.list"
              className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40"
            />
          </div>
          <div>
            <div className="text-[10px] text-muted mb-1">调度间隔 (分钟)</div>
            <input
              type="number" min={1} value={schedule} onChange={e => setSchedule(Number(e.target.value))}
              className="w-full rounded-btn border border-border bg-elevated px-2 py-1.5 text-[10px] font-mono text-foreground"
            />
          </div>
        </div>

        <div>
          <div className="text-[10px] text-muted mb-1">字段映射 (外部名 → 内部名，JSON，可选)</div>
          <textarea
            value={fieldMapStr} onChange={e => setFieldMapStr(e.target.value)}
            placeholder='{"code": "symbol", "val": "score"}'
            rows={2}
            className="w-full rounded-btn border border-border bg-elevated px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none"
          />
        </div>
      </div>

      {/* ===== 分区 ②: 定时拉取状态 ===== */}
      <div className="rounded-card border border-border/60 bg-elevated/30 p-2.5 space-y-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
            <Clock className="h-3 w-3 text-muted" />
            <span>定时拉取</span>
          </div>
          {/* 自定义 Toggle 开关 */}
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            disabled={toggling}
            onClick={() => handleToggle(!enabled)}
            className={`relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors duration-200 disabled:opacity-50 ${
              enabled ? 'bg-accent' : 'bg-border'
            }`}
          >
            <span
              className={`inline-block h-3 w-3 transform rounded-full bg-white shadow transition-transform duration-200 ${
                enabled ? 'translate-x-3.5' : 'translate-x-0.5'
              }`}
            />
          </button>
        </div>

        {/* 状态文案 */}
        <div className="text-[10px] leading-relaxed">
          {enabled ? (
            <div className="space-y-0.5">
              <div className="flex items-center gap-1 text-accent">
                <span className="h-1 w-1 rounded-full bg-accent animate-pulse" />
                <span>已启用 · 每 {schedule} 分钟</span>
              </div>
              {pull?.next_run && fmtTime(pull.next_run) && (
                <div className="flex items-center gap-1 text-muted">
                  <Calendar className="h-2.5 w-2.5" />
                  <span>下次：{fmtTime(pull.next_run)}</span>
                </div>
              )}
            </div>
          ) : (
            <div className="text-muted">未启用 · 仅手动执行</div>
          )}
        </div>

        {/* 上次执行结果 */}
        {pull?.last_run && (
          <div className="flex items-start gap-1.5 pt-1.5 border-t border-border/40">
            {pull.last_status === 'success' ? (
              <CheckCircle2 className="h-3 w-3 text-emerald-500 shrink-0 mt-px" />
            ) : (
              <AlertCircle className="h-3 w-3 text-danger shrink-0 mt-px" />
            )}
            <div className="min-w-0 flex-1">
              <div className={`text-[10px] font-medium ${pull.last_status === 'success' ? 'text-emerald-500' : 'text-danger'}`}>
                {pull.last_message || (pull.last_status === 'success' ? '成功' : '失败')}
              </div>
              {fmtTime(pull.last_run) && (
                <div className="text-[9px] text-muted">{fmtTime(pull.last_run)}</div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* ===== 分区 ③: 操作按钮 ===== */}
      <div className="space-y-2">
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={handleTest}
            disabled={testing || !url}
            className="inline-flex items-center justify-center gap-1 px-2 py-2 rounded-btn border border-border bg-elevated text-xs text-foreground hover:bg-border/30 disabled:opacity-40 transition-colors"
          >
            {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
            测试
          </button>
          <button
            onClick={handleRun}
            disabled={running || !url}
            className="inline-flex items-center justify-center gap-1 px-2 py-2 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors"
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
            立即执行
          </button>
        </div>
        <button
          onClick={() => handleSave(false)}
          disabled={saving || !url}
          className="w-full inline-flex items-center justify-center gap-1 py-2 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors"
        >
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
          保存配置
        </button>
      </div>

      {/* ===== 结果展示 ===== */}
      {runResult && (
        <div className="rounded-card border border-emerald-500/30 bg-emerald-500/[0.06] p-2.5 flex items-center justify-between text-[10px]">
          <span className="text-emerald-500 font-medium flex items-center gap-1">
            <CheckCircle2 className="h-3 w-3" />拉取成功
          </span>
          <span className="text-secondary">{runResult.rows} 行 · {runResult.date}</span>
        </div>
      )}

      {testResult && (
        <div className="rounded-card border border-accent/30 bg-accent/[0.04] p-2.5 space-y-1.5">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-accent font-medium">测试成功</span>
            <span className="text-secondary">{testResult.total_rows} 行</span>
          </div>
          {!testResult.has_symbol && (
            <div className="text-[10px] text-amber-500">数据缺少 symbol 字段，请配置字段映射</div>
          )}
          {testResult.preview.length > 0 && (
            <pre className="text-[9px] font-mono text-muted bg-elevated rounded px-2 py-1.5 overflow-x-auto max-h-32">
              {JSON.stringify(testResult.preview, null, 2)}
            </pre>
          )}
        </div>
      )}

      {error && (
        <div className="text-[10px] text-danger text-center bg-danger/[0.06] rounded-btn py-1.5">
          {error}
        </div>
      )}
    </div>
  )
}
