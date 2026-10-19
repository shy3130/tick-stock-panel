import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Loader2, Search, Check, Clock, Zap, Settings2, AlertCircle, CheckCircle2, Calendar, History, Layers, X, Database } from 'lucide-react'
import { api, type ExtDataBackfillResult, type ExtDataConfig, type ExtPullAuth } from '@/lib/api'
import { toast } from '@/components/Toast'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { SectionHead, INPUT_CLS } from './dialogShared'

const AUTH_TYPE_LABELS: Record<ExtPullAuth['type'], string> = {
  none: '无',
  bearer: 'Bearer Token',
  header: '自定义请求头',
  query: 'URL 查询参数',
}

const WEEKDAY_CN = ['一', '二', '三', '四', '五', '六', '日']

// 格式化时间显示
function fmtTime(iso: string | null | undefined) {
  if (!iso) return null
  const d = new Date(iso)
  if (isNaN(d.getTime())) return null
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${mi}`
}

/**
 * 卡片内的紧凑拉取控制: 状态 + 开关 + 立即执行 + 打开设置弹窗。
 * 完整配置在 ExtPullConfigDialog (宽弹窗) 里 — 窄卡片塞不下整套表单。
 */
export function ExtDataPullPanel({ config, onSaved }: {
  config: ExtDataConfig
  onSaved: () => void
}) {
  const pull = config.pull
  const [showConfig, setShowConfig] = useState(false)
  const [toggling, setToggling] = useState(false)
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<{ rows: number; date: string } | null>(null)
  const [error, setError] = useState('')

  const enabled = optimistic ?? (pull?.enabled ?? false)

  // 有效超时 (5~300 归一): 按已保存配置计算, 与后端口径一致
  const savedTimeout = pull?.timeout_seconds ?? 30
  const effTimeoutSec = Number.isFinite(savedTimeout)
    && savedTimeout >= 5 && savedTimeout <= 300
    ? savedTimeout : 30

  const scheduleSummary = pull?.schedule_time
    ? `${
      pull.schedule_period === 'weekly' ? `每周${WEEKDAY_CN[(pull.schedule_day ?? 1) - 1]}`
        : pull.schedule_period === 'monthly' ? `每月 ${pull.schedule_day ?? 1} 号`
          : '每天'
    } ${pull.schedule_time}${pull.schedule_trading_only ? ' · 仅交易日' : ''}`
    : `每 ${pull?.schedule_minutes ?? 1440} 分钟`

  // 从已保存配置原样重建 payload 仅切换 enabled — 不掺入设置弹窗里未保存的草稿;
  // page_start / fanout / auth.key_ref 等 UI 不暴露的字段透传保留
  const buildTogglePayload = (next: boolean) => {
    if (!pull) return null
    return {
      url: pull.url,
      method: pull.method,
      headers: pull.headers,
      body: pull.body ?? undefined,
      response_path: pull.response_path,
      field_map: pull.field_map,
      auth: pull.auth ?? undefined,
      schedule_minutes: pull.schedule_minutes,
      enabled: next,
      schedule_time: pull.schedule_time ?? null,
      schedule_period: pull.schedule_period ?? null,
      schedule_day: pull.schedule_day ?? null,
      schedule_trading_only: pull.schedule_trading_only ?? false,
      time_window_start: pull.time_window_start ?? null,
      time_window_end: pull.time_window_end ?? null,
      date_param: pull.date_param ?? null,
      date_format: pull.date_format,
      time_field: pull.time_field ?? null,
      timeout_seconds: pull.timeout_seconds,
      page_param: pull.page_param ?? null,
      page_size_param: pull.page_size_param ?? null,
      page_size: pull.page_size,
      page_start: pull.page_start,
      max_pages: pull.max_pages,
      fanout: pull.fanout ?? null,
    }
  }

  // 开关 toggle: 自动保存全量配置 (切换 enabled), 后端 refresh 后立即首次拉取
  const handleToggle = (next: boolean) => {
    if (toggling) return
    if (next && !pull?.url?.trim()) {
      toast('请先在「拉取设置」中填写 URL', 'error')
      return
    }
    const payload = buildTogglePayload(next)
    if (!payload) return
    setToggling(true); setError(''); setOptimistic(next)
    api.extDataPullConfig(config.id, payload)
      .then(() => {
        onSaved()
        toast(next ? '定时拉取已启用 · 立即执行首次拉取' : '定时拉取已关闭', 'success')
      })
      .catch(e => setError(e.message || '切换失败'))
      .finally(() => { setToggling(false); setOptimistic(null) })
  }

  const handleRun = () => {
    setRunning(true); setError(''); setRunResult(null)
    api.extDataPullRun(config.id, effTimeoutSec)
      .then(r => {
        setRunResult({ rows: r.rows, date: r.date })
        onSaved()
        toast(`拉取成功 · ${r.rows} 行`, 'success')
      })
      .catch(e => setError(e.message || '执行失败'))
      .finally(() => setRunning(false))
  }

  return (
    <div className="space-y-2">
      {/* 状态 + 总开关 */}
      <div className="rounded-lg border border-border/60 bg-elevated/30 px-3 py-2.5 space-y-1.5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
            <Clock className="h-3 w-3 text-muted" />
            <span>定时拉取</span>
          </div>
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

        <div className="text-[10px] leading-relaxed">
          {enabled ? (
            <div className="space-y-0.5">
              <div className="flex items-center gap-1 text-accent">
                <span className="h-1 w-1 rounded-full bg-accent animate-pulse" />
                <span>已启用 · {scheduleSummary}</span>
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

        {pull?.last_run && (
          <div className="flex items-start gap-1.5 pt-1.5 border-t border-border/40">
            {pull.last_status === 'success' ? (
              <CheckCircle2 className="h-3 w-3 text-emerald-500 shrink-0 mt-px" />
            ) : (
              <AlertCircle className="h-3 w-3 text-danger shrink-0 mt-px" />
            )}
            <div className="min-w-0 flex-1">
              <div className={`text-[10px] font-medium truncate ${pull.last_status === 'success' ? 'text-emerald-500' : 'text-danger'}`}>
                {pull.last_message || (pull.last_status === 'success' ? '成功' : '失败')}
              </div>
              {fmtTime(pull.last_run) && (
                <div className="text-[9px] text-muted">{fmtTime(pull.last_run)}</div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 操作: 立即执行 + 打开设置 */}
      <div className="grid grid-cols-2 gap-1.5">
        <button
          onClick={handleRun}
          disabled={running || !pull?.url}
          className="inline-flex items-center justify-center gap-1 h-9 rounded-lg bg-accent/15 border border-accent/25 text-accent text-xs font-medium hover:bg-accent/20 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
        >
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
          立即执行
        </button>
        <button
          onClick={() => setShowConfig(true)}
          disabled={!pull?.url}
          title={pull?.url ? undefined : '请先通过「新建扩展数据 → URL」创建拉取配置'}
          className="inline-flex items-center justify-center gap-1 h-9 rounded-lg border border-border bg-elevated text-xs text-foreground hover:bg-border/30 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
        >
          <Settings2 className="h-3.5 w-3.5" />
          拉取设置…
        </button>
      </div>

      {runResult && (
        <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/[0.06] p-2.5 flex items-center justify-between text-[10px]">
          <span className="text-emerald-500 font-medium flex items-center gap-1">
            <CheckCircle2 className="h-3 w-3" />拉取成功
          </span>
          <span className="text-secondary">{runResult.rows} 行 · {runResult.date}</span>
        </div>
      )}

      {error && (
        <div className="text-[10px] text-danger text-center bg-danger/[0.06] rounded-lg py-1.5">
          {error}
        </div>
      )}

      {showConfig && (
        <ExtPullConfigDialog
          config={config}
          onSaved={onSaved}
          onClose={() => setShowConfig(false)}
        />
      )}
    </div>
  )
}

/** 完整拉取设置弹窗: 请求 / 调度 / 鉴权 / 数据解析 / 历史回补, 底部固定操作栏 */
function ExtPullConfigDialog({ config, onSaved, onClose }: {
  config: ExtDataConfig
  onSaved: () => void
  onClose: () => void
}) {
  const backdrop = useDialogBackdrop(onClose)
  const pull = config.pull
  const fanout = pull?.fanout
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
  // 调度二选一 (UI 态): 间隔制 / 固定时刻制。旧版「填了时刻就把间隔框禁用」
  // 的隐式联动发现性差, 改为显式切换; 切回间隔制时清空固定时刻
  const [scheduleMode, setScheduleMode] = useState<'interval' | 'fixed'>(pull?.schedule_time ? 'fixed' : 'interval')
  const [scheduleTime, setScheduleTime] = useState(pull?.schedule_time ?? '')
  const [schedulePeriod, setSchedulePeriod] = useState<'daily' | 'weekly' | 'monthly'>(pull?.schedule_period ?? 'daily')
  const [scheduleDay, setScheduleDay] = useState(pull?.schedule_day ?? 1)
  const [scheduleTradingOnly, setScheduleTradingOnly] = useState(pull?.schedule_trading_only ?? false)
  const [timeWindowStart, setTimeWindowStart] = useState(pull?.time_window_start ?? '')
  const [timeWindowEnd, setTimeWindowEnd] = useState(pull?.time_window_end ?? '')
  const [dateParam, setDateParam] = useState(pull?.date_param ?? '')
  const [dateFormat, setDateFormat] = useState(pull?.date_format ?? 'iso')
  const [timeField, setTimeField] = useState(pull?.time_field ?? '')
  const [timeoutSec, setTimeoutSec] = useState(pull?.timeout_seconds ?? 30)
  // 分页协议 (仅 GET): 页码参数名配置后按页循环拉取
  const [pageParam, setPageParam] = useState(pull?.page_param ?? '')
  const [pageSizeParam, setPageSizeParam] = useState(pull?.page_size_param ?? '')
  const [pageSize, setPageSize] = useState(pull?.page_size ?? 0)
  const [maxPages, setMaxPages] = useState(pull?.max_pages ?? 20)
  const [enabled, setEnabled] = useState(pull?.enabled ?? false)

  // 接口鉴权: 方式入 pull 配置; Key 本体只存后端 secrets.json
  const [authType, setAuthType] = useState<ExtPullAuth['type']>(pull?.auth?.type ?? 'none')
  const [authHeader, setAuthHeader] = useState(pull?.auth?.header ?? 'Authorization')
  const [authParam, setAuthParam] = useState(pull?.auth?.param ?? 'token')
  const [apiKey, setApiKey] = useState('')
  const [keyDirty, setKeyDirty] = useState(false)
  const [keyInfo, setKeyInfo] = useState<{ key_set: boolean; masked_key: string } | null>(null)

  useEffect(() => {
    api.extDataApiKey(config.id).then(setKeyInfo).catch(() => {})
  }, [config.id])

  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [running, setRunning] = useState(false)
  const [runResult, setRunResult] = useState<{ rows: number; date: string } | null>(null)
  const [testResult, setTestResult] = useState<{ total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean } | null>(null)
  const [error, setError] = useState('')

  // 历史回补 (仅 timeseries + 接口支持按日查询)
  const today = new Date().toISOString().slice(0, 10)
  const monthAgo = new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10)
  const [bfStart, setBfStart] = useState(monthAgo)
  const [bfEnd, setBfEnd] = useState(today)
  const [bfRunning, setBfRunning] = useState(false)
  const [bfResult, setBfResult] = useState<ExtDataBackfillResult | null>(null)

  // 解析 JSON 输入, 失败时设置 error 并返回 null
  const parseJson = (str: string, label: string): Record<string, string> | undefined | null => {
    if (!str.trim()) return undefined
    try { return JSON.parse(str) }
    catch { setError(`${label} 不是有效 JSON`); return null }
  }

  // 有效超时 (5~300 归一): 后端配置、前端 fetch 的 timeoutMs 共用同一口径
  const effTimeoutSec = Number.isFinite(timeoutSec) && timeoutSec >= 5 && timeoutSec <= 300 ? timeoutSec : 30

  // 构建保存 payload (复用当前编辑态), enabledOverride 用于开关自动保存
  const buildPayload = (enabledOverride?: boolean) => {
    if (scheduleMode === 'fixed' && !scheduleTime) {
      setError('固定时刻模式请先选择时刻（或切回「按间隔」）')
      return null
    }
    const headers = parseJson(headerStr, 'Headers')
    if (headers === null) return null
    const field_map = parseJson(fieldMapStr, '字段映射')
    if (field_map === null) return null
    const auth: ExtPullAuth = {
      type: authType,
      header: authHeader.trim() || 'Authorization',
      param: authParam.trim() || 'token',
      // UI 不暴露 key_ref (复用其他插件的凭据): 保留已有值, 避免普通保存把它抹掉
      ...(pull?.auth?.key_ref ? { key_ref: pull.auth.key_ref } : {}),
    }
    return {
      url, method, headers, body: body || undefined,
      response_path: responsePath, field_map, auth,
      schedule_minutes: schedule, enabled: enabledOverride ?? enabled,
      schedule_time: scheduleMode === 'fixed' ? scheduleTime : null,
      schedule_period: scheduleMode === 'fixed' ? schedulePeriod : null,
      schedule_day: scheduleMode === 'fixed' && schedulePeriod !== 'daily' ? scheduleDay : null,
      schedule_trading_only: scheduleMode === 'fixed' ? scheduleTradingOnly : false,
      time_window_start: timeWindowStart || null,
      time_window_end: timeWindowEnd || null,
      date_param: dateParam.trim() || null,
      date_format: dateFormat,
      time_field: timeField.trim() || null,
      timeout_seconds: effTimeoutSec,
      page_param: pageParam.trim() || null,
      page_size_param: pageSizeParam.trim() || null,
      page_size: pageSize > 0 ? pageSize : 0,
      page_start: pull?.page_start ?? 1,   // UI 不暴露, 保留已有值 (从 0 计数的接口手改 config.json)
      max_pages: maxPages >= 1 && maxPages <= 200 ? maxPages : 20,
      // 两级拉取配方同样不在 UI 编辑: 保留已有值 (出厂配方 / 手改 config.json)
      fanout: pull?.fanout ?? null,
    }
  }

  // Key 输入有改动时随配置一起保存 (空输入=清除); 未改动则跳过
  const saveKeyIfNeeded = () =>
    keyDirty
      ? api.extDataApiKeySet(config.id, apiKey).then(r => {
        setKeyInfo({ key_set: r.key_set, masked_key: r.masked_key })
        setKeyDirty(false)
        setApiKey('')
      })
      : Promise.resolve()

  const handleSave = (silent = false) => {
    const payload = buildPayload()
    if (!payload) return
    setSaving(true); setError('')
    saveKeyIfNeeded()
      .then(() => api.extDataPullConfig(config.id, payload))
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
    saveKeyIfNeeded()
      .then(() => api.extDataPullConfig(config.id, payload))
      .then(() => api.extDataPullTest(config.id, effTimeoutSec))
      .then(r => { setTestResult(r); onSaved() })
      .catch(e => setError(e.message || '测试失败'))
      .finally(() => setTesting(false))
  }

  const handleRun = () => {
    setRunning(true); setError(''); setRunResult(null)
    api.extDataPullRun(config.id, effTimeoutSec)
      .then(r => {
        setRunResult({ rows: r.rows, date: r.date })
        onSaved()
        toast(`拉取成功 · ${r.rows} 行`, 'success')
      })
      .catch(e => setError(e.message || '执行失败'))
      .finally(() => setRunning(false))
  }

  const handleBackfill = () => {
    setBfRunning(true); setError(''); setBfResult(null)
    // 服务端逐日串行拉取, 前端超时按 天数×单日超时 估算 (+30s 写盘缓冲)
    const daySpan = Math.max(1, Math.round((Date.parse(bfEnd) - Date.parse(bfStart)) / 86400_000) + 1)
    api.extDataBackfill(config.id, bfStart, bfEnd, daySpan * effTimeoutSec * 1000 + 30_000)
      .then(r => {
        setBfResult(r)
        onSaved()
        if (r.failed.length === 0) toast(`回补完成 · 写入 ${r.fetched} 日 ${r.rows_written} 行`, 'success')
        else toast(`回补完成 · ${r.failed.length} 日失败 (见详情)`, 'error')
      })
      .catch(e => setError(e.message || '回补失败'))
      .finally(() => setBfRunning(false))
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
    saveKeyIfNeeded()
      .then(() => api.extDataPullConfig(config.id, payload))
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

  const scheduleSummary = scheduleMode === 'fixed' && scheduleTime
    ? `${
      schedulePeriod === 'weekly' ? `每周${WEEKDAY_CN[scheduleDay - 1]}`
        : schedulePeriod === 'monthly' ? `每月 ${scheduleDay} 号`
          : '每天'
    } ${scheduleTime}${scheduleTradingOnly ? ' · 仅交易日' : ''}`
    : `每 ${schedule} 分钟`

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" {...backdrop} />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative rounded-2xl border border-border bg-surface shadow-2xl mx-4 w-full max-w-2xl max-h-[88vh] flex flex-col overflow-hidden"
      >
        <div className="px-6 pt-5 pb-4 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="mt-0.5 p-2 rounded-xl bg-amber-500/10 text-amber-500 border border-amber-500/20 shrink-0">
              <Database className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <h3 className="text-[15px] leading-6 font-semibold text-foreground">拉取设置</h3>
              <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                <span className="text-[11px] font-mono text-muted">{config.id}</span>
                <span className={`inline-flex items-center gap-1 px-1.5 py-px rounded text-[10px] border ${
                  enabled
                    ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-500'
                    : 'bg-elevated border-border/60 text-muted'
                }`}>
                  {enabled ? '定时中' : '未启用'} · {scheduleSummary}
                </span>
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1.5 rounded-lg text-secondary hover:text-foreground hover:bg-elevated focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors shrink-0"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 pb-6 space-y-6">
          {/* 两级拉取配方: 目录 → 逐条目明细。展示全部关键项, 便于照抄接入自有源 */}
          {fanout && (
            <div className="rounded-xl border border-accent/25 bg-accent/[0.05] px-4 py-3">
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
                <Layers className="h-3 w-3 text-accent/80" />
                <span>两级拉取 (目录 → 逐条目)</span>
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
                接口只提供「类别清单」+「单类别成员」时用这个配方：先取第一级目录，再按
                <span className="mx-0.5 font-mono text-[10px] text-secondary">{fanout.param}</span>
                参数并发拉每个条目的成员，最后反转为「标的 → {fanout.value_col}」。接入自有源时在「新建扩展数据 → URL」填写第一级地址，配方存于表的
                <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px]">config.json</span>
                的 <span className="font-mono text-[10px]">pull.fanout</span> 字段；详见
                <span className="mx-0.5 rounded bg-elevated/70 px-1 py-px font-mono text-[10px]">docs/custom-data-source.md</span>
              </p>
              <div className="mt-2 space-y-1 text-[10px] leading-relaxed">
                {([
                  ['第二级 URL', fanout.url],
                  ['参数 / 取值字段', `${fanout.param} ← ${fanout.id_field}`],
                  ['成员提取路径', fanout.items_path || '(响应本身即数组)'],
                  ['成员字段 → 列', `${fanout.member_id_field} → ${fanout.symbol_col}${fanout.member_label_field ? `, ${fanout.member_label_field} → ${fanout.name_col}` : ''}`],
                  ['拼接列 / 分隔符', `${fanout.value_col} · "${fanout.separator || ';'}"`],
                  ['并发 / 重试', `${fanout.concurrency ?? 8} · ${fanout.retries ?? 1}`],
                ] as const).map(([k, v]) => (
                  <div key={k} className="flex gap-2">
                    <span className="w-[92px] shrink-0 text-muted">{k}</span>
                    <span className="min-w-0 flex-1 break-all font-mono text-secondary">{v}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 01 请求 */}
          <section>
            <SectionHead num="01" title={fanout ? '第一级 (目录) 请求' : '请求'}>
              <span className="text-[10px] text-muted/70">保存后「测试」会按此配置真实请求一次</span>
            </SectionHead>
            <div className="flex gap-2">
              <select
                value={method} onChange={e => setMethod(e.target.value)}
                className="h-9 shrink-0 rounded-lg border border-border bg-base px-2.5 text-xs font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
              >
                <option value="GET">GET</option>
                <option value="POST">POST</option>
              </select>
              <input
                value={url} onChange={e => setUrl(e.target.value)}
                placeholder="https://api.example.com/data"
                className="h-9 flex-1 min-w-0 rounded-lg border border-border bg-base px-3 text-xs font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
              />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2">
              <div>
                <div className="text-[10px] text-muted mb-1">Headers（JSON，可选）</div>
                <textarea
                  value={headerStr} onChange={e => setHeaderStr(e.target.value)}
                  placeholder='{"X-Custom": "value"}'
                  rows={3}
                  className="w-full rounded-lg border border-border bg-base px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                />
              </div>
              <div>
                <div className="text-[10px] text-muted mb-1">
                  请求体（JSON，可选）{method !== 'POST' && <span className="text-muted/60">· 仅 POST</span>}
                </div>
                <textarea
                  value={body} onChange={e => setBody(e.target.value)}
                  placeholder='{"page": 1}'
                  rows={3}
                  disabled={method !== 'POST'}
                  className="w-full rounded-lg border border-border bg-base px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none disabled:opacity-40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-[11px] font-medium text-secondary mb-1.5">
                拉取超时（秒）
                <span className="ml-1.5 text-[10px] font-normal text-muted">5~300 · 大响应接口可调高</span>
              </div>
              <input
                type="number" min={5} max={300} step={5}
                value={timeoutSec}
                onChange={e => setTimeoutSec(Number(e.target.value))}
                title="单次拉取/测试/回补请求的超时, 默认 30 秒"
                className={`${INPUT_CLS} font-mono tabular-nums`}
              />
            </div>
          </section>

          {/* 02 调度 */}
          <section>
            <SectionHead num="02" title="调度">
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                disabled={toggling}
                onClick={() => handleToggle(!enabled)}
                className="relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors duration-200 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                title={enabled ? '点击关闭定时拉取 (立即生效)' : '点击启用定时拉取 (保存当前编辑态并立即首次拉取)'}
              >
                <span className={`absolute inset-0 rounded-full ${enabled ? 'bg-accent' : 'bg-border'}`} />
                <span
                  className={`relative inline-block h-3 w-3 transform rounded-full bg-white shadow transition-transform duration-200 ${
                    enabled ? 'translate-x-3.5' : 'translate-x-0.5'
                  }`}
                />
              </button>
            </SectionHead>

            <div className="rounded-xl border border-border/60 bg-elevated/20 p-3.5 space-y-3">
              {/* 调度方式二选一: 显式切换, 不再用「填了时刻就禁用间隔」的隐式联动 */}
              <div className="grid grid-cols-2 gap-1 rounded-lg bg-elevated/50 p-1 border border-border/40">
                <button
                  onClick={() => { setScheduleMode('interval'); setScheduleTime('') }}
                  className={`h-8 rounded-md text-[11px] font-medium transition-all ${
                    scheduleMode === 'interval' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary hover:bg-elevated/40'
                  }`}
                >
                  按间隔
                </button>
                <button
                  onClick={() => setScheduleMode('fixed')}
                  className={`h-8 rounded-md text-[11px] font-medium transition-all ${
                    scheduleMode === 'fixed' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary hover:bg-elevated/40'
                  }`}
                >
                  固定时刻
                </button>
              </div>

              {scheduleMode === 'interval' ? (
                <div className="space-y-3">
                  <div className="grid grid-cols-3 gap-2">
                    <div>
                      <div className="text-[10px] text-muted mb-1">间隔（分钟）</div>
                      <input
                        type="number" min={1} value={schedule} onChange={e => setSchedule(Number(e.target.value))}
                        title="每隔 N 分钟拉取一次 (从启用时刻起算)"
                        className="w-full h-8 px-2.5 rounded-lg border border-border bg-base text-[11px] font-mono tabular-nums text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
                      />
                    </div>
                    <div>
                      <div className="text-[10px] text-muted mb-1">窗口起（留空=不限）</div>
                      <input
                        type="time" value={timeWindowStart} onChange={e => setTimeWindowStart(e.target.value)}
                        title="每日允许拉取的时间窗口"
                        className="w-full h-8 px-2 rounded-lg border border-border bg-base text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
                      />
                    </div>
                    <div>
                      <div className="text-[10px] text-muted mb-1">窗口止（留空=不限）</div>
                      <input
                        type="time" value={timeWindowEnd} onChange={e => setTimeWindowEnd(e.target.value)}
                        title="每日允许拉取的时间窗口"
                        className="w-full h-8 px-2 rounded-lg border border-border bg-base text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
                      />
                    </div>
                  </div>
                  <div className="text-[10px] text-muted/70 leading-relaxed">
                    间隔制锁相在启用时刻 — 「每天 09:30」这类日程请用固定时刻。
                  </div>
                </div>
              ) : (
                <div className="space-y-2.5">
                  <div className="grid grid-cols-3 gap-2">
                    <select
                      value={schedulePeriod}
                      onChange={e => setSchedulePeriod(e.target.value as 'daily' | 'weekly' | 'monthly')}
                      className="h-8 rounded-lg border border-border bg-base px-2 text-[11px] text-foreground cursor-pointer focus:outline-none focus:ring-1 focus:ring-accent/25"
                    >
                      <option value="daily">每日</option>
                      <option value="weekly">每周</option>
                      <option value="monthly">每月</option>
                    </select>
                    <select
                      value={String(scheduleDay)}
                      onChange={e => setScheduleDay(Number(e.target.value))}
                      disabled={schedulePeriod === 'daily'}
                      title={schedulePeriod === 'weekly' ? '每周几拉取' : '每月几号拉取 (短月无该日则该月跳过)'}
                      className="h-8 rounded-lg border border-border bg-base px-2 text-[11px] text-foreground cursor-pointer focus:outline-none focus:ring-1 focus:ring-accent/25 disabled:opacity-40"
                    >
                      {(schedulePeriod === 'weekly'
                        ? ['周一', '周二', '周三', '周四', '周五', '周六', '周日'].map((t, i) => [i + 1, t] as [number, string])
                        : Array.from({ length: 31 }, (_, i) => [i + 1, `${i + 1} 号`] as [number, string])
                      ).map(([v, t]) => (
                        <option key={v} value={String(v)}>{t}</option>
                      ))}
                    </select>
                    <div className="flex items-center gap-1.5">
                      <input
                        type="time" value={scheduleTime} onChange={e => setScheduleTime(e.target.value)}
                        title="如 09:30 — 周期日该时刻拉取一次; 启用时若当天已过该时刻且未拉过会立即补拉一次"
                        className="h-8 flex-1 min-w-0 rounded-lg border border-border bg-base px-2 text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
                      />
                      {scheduleTime && (
                        <button
                          type="button" onClick={() => setScheduleTime('')}
                          className="h-8 shrink-0 px-2 rounded-lg border border-border bg-base text-[10px] text-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                        >
                          清除
                        </button>
                      )}
                    </div>
                  </div>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox" checked={scheduleTradingOnly}
                      onChange={e => setScheduleTradingOnly(e.target.checked)}
                      title="跳过周六日; 法定节假日无法离线判定, 仍会按期尝试 (拉取幂等, 失败仅记状态)"
                      className="h-3 w-3 shrink-0 accent-amber-500"
                    />
                    <span className="text-[10px] text-secondary">
                      仅交易日 — 跳过周六日
                      <span className="text-muted/70">（节假日无法离线判定，仍会按期尝试）</span>
                    </span>
                  </label>
                  <div className="text-[10px] text-muted/70 leading-relaxed">
                    北京时间墙钟对齐，忽略间隔与时间窗口。
                  </div>
                </div>
              )}

              <div className="text-[10px] text-muted border-t border-border/40 pt-2">
                当前生效：<span className="text-secondary">{scheduleSummary}</span>
              </div>
            </div>
          </section>

          {/* 03 接口鉴权 (API Key) */}
          <section>
            <SectionHead num="03" title="接口鉴权（API Key）">
              {authType !== 'none' && keyInfo && (
                <span className={`text-[10px] ${keyInfo.key_set ? 'text-emerald-500' : 'text-amber-500'}`}>
                  {keyInfo.key_set ? `已设置 ${keyInfo.masked_key}` : '未设置 Key'}
                </span>
              )}
            </SectionHead>
            <div className={`grid gap-3 ${authType === 'none' ? '' : 'grid-cols-3'}`}>
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">鉴权方式</div>
                <select
                  value={authType}
                  onChange={e => setAuthType(e.target.value as ExtPullAuth['type'])}
                  className={`${INPUT_CLS} cursor-pointer`}
                >
                  {Object.entries(AUTH_TYPE_LABELS).map(([v, label]) => (
                    <option key={v} value={v}>{label}</option>
                  ))}
                </select>
              </div>
              {authType === 'query' && (
                <div>
                  <div className="text-[11px] font-medium text-secondary mb-1.5">参数名</div>
                  <input
                    value={authParam} onChange={e => setAuthParam(e.target.value)}
                    placeholder="token"
                    className={`${INPUT_CLS} font-mono`}
                  />
                </div>
              )}
              {(authType === 'bearer' || authType === 'header') && (
                <div>
                  <div className="text-[11px] font-medium text-secondary mb-1.5">请求头名称</div>
                  <input
                    value={authHeader} onChange={e => setAuthHeader(e.target.value)}
                    placeholder="Authorization"
                    className={`${INPUT_CLS} font-mono`}
                  />
                </div>
              )}
              {authType !== 'none' && (
                <div>
                  <div className="text-[11px] font-medium text-secondary mb-1.5">API Key</div>
                  <input
                    type="password"
                    value={apiKey}
                    onChange={e => { setApiKey(e.target.value); setKeyDirty(true) }}
                    autoComplete="new-password"
                    placeholder={keyInfo?.key_set ? '输入新 Key 覆盖 · 清空保存 = 删除' : '输入 API Key'}
                    className={`${INPUT_CLS} font-mono`}
                  />
                </div>
              )}
            </div>
            {authType !== 'none' && (
              <div className="mt-2 text-[10px] text-muted/70">
                Key 仅存本机 secrets.json（不写入配置文件、不随配置导出）；随「保存配置 / 测试」一起生效。
              </div>
            )}
          </section>

          {/* 04 数据解析 */}
          <section>
            <SectionHead num="04" title="数据解析">
              <span className="text-[10px] text-muted/70">从响应 JSON 到表行的映射</span>
            </SectionHead>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <div className="text-[11px] font-medium text-secondary mb-1.5">响应数据路径</div>
                  <input
                    value={responsePath} onChange={e => setResponsePath(e.target.value)}
                    placeholder="data.list"
                    className={`${INPUT_CLS} font-mono`}
                  />
                </div>
                <div>
                  <div className="text-[11px] font-medium text-secondary mb-1.5">
                    时间字段
                    <span className="ml-1.5 text-[10px] font-normal text-muted">日内多行数据填，如 ts</span>
                  </div>
                  <input
                    value={timeField} onChange={e => setTimeField(e.target.value)}
                    placeholder="ts · 留空=每日快照表"
                    title="配置后同一代码允许一天多行, 按代码+时间列去重"
                    className={`${INPUT_CLS} font-mono`}
                  />
                </div>
              </div>
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">
                  字段映射（外部名 → 内部名，JSON，可选）
                </div>
                <textarea
                  value={fieldMapStr} onChange={e => setFieldMapStr(e.target.value)}
                  placeholder='{"code": "symbol", "val": "score"}'
                  rows={2}
                  className="w-full rounded-lg border border-border bg-base px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                />
              </div>
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">
                  日期参数名
                  <span className="ml-1.5 text-[10px] font-normal text-muted">接口支持按日查询时填，如 date · 配合历史回补</span>
                </div>
                <div className="flex gap-2">
                  <input
                    value={dateParam} onChange={e => setDateParam(e.target.value)}
                    placeholder="date · 留空=接口只有当日快照"
                    className="h-9 flex-1 min-w-0 rounded-lg border border-border bg-base px-3 text-xs font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                  />
                  <select
                    aria-label="日期参数值格式"
                    value={dateFormat} onChange={e => setDateFormat(e.target.value)}
                    disabled={!dateParam.trim()}
                    title="日期参数值的序列化格式; 时间戳 = 该交易日北京时间 00:00:00"
                    className="h-9 shrink-0 rounded-lg border border-border bg-base px-2 text-[11px] text-secondary focus:outline-none focus:ring-1 focus:ring-accent/25 disabled:opacity-40"
                  >
                    <option value="iso">YYYY-MM-DD</option>
                    <option value="compact">YYYYMMDD</option>
                    <option value="ts_s">秒时间戳</option>
                    <option value="ts_ms">毫秒时间戳</option>
                  </select>
                </div>
              </div>
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">
                  分页（仅 GET）
                    <span className="ml-1.5 text-[10px] font-normal text-muted">留空页码参数=单次请求</span>
                  </div>
                  <div className="grid grid-cols-2 gap-1.5">
                    <input
                      value={pageParam} onChange={e => setPageParam(e.target.value)}
                      placeholder="页码参数 · page"
                      title="配置后按页循环拉取, 直到空页/最后一页/达上限"
                      className="h-8 rounded-lg border border-border bg-base px-2 text-[10px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                    />
                    <input
                      value={pageSizeParam} onChange={e => setPageSizeParam(e.target.value)}
                      placeholder="条数参数 · pageSize"
                      title="配合每页条数一起发送; 也用于短页判停"
                      className="h-8 rounded-lg border border-border bg-base px-2 text-[10px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                    />
                    <input
                      type="number" min={1}
                      value={pageSize}
                      onChange={e => setPageSize(Number(e.target.value))}
                      placeholder="每页条数"
                      title="每页条数值 (>0 且已填参数名才发送)"
                      className="h-8 rounded-lg border border-border bg-base px-2 text-[10px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                    />
                    <input
                      type="number" min={1} max={200}
                      value={maxPages}
                      onChange={e => setMaxPages(Number(e.target.value))}
                      placeholder="最多页 · 20"
                      title="安全上限, 防止接口永远返回数据拖死拉取循环"
                      className="h-8 rounded-lg border border-border bg-base px-2 text-[10px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                    />
                  </div>
              </div>
            </div>
          </section>

          {/* 05 历史回补 (仅 timeseries + 接口支持按日查询) */}
          {config.mode === 'timeseries' && (
            <section>
              <SectionHead num="05" title="历史回补">
                {!dateParam.trim() && <span className="text-[10px] text-amber-500">需先填日期参数名并保存</span>}
              </SectionHead>
              <div className="rounded-xl border border-border/60 bg-elevated/20 p-3.5 space-y-2">
                <div className="flex items-center gap-2">
                  <input
                    type="date" value={bfStart} onChange={e => setBfStart(e.target.value)}
                    className="h-9 flex-1 min-w-0 rounded-lg border border-border bg-base px-2.5 text-xs font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
                  />
                  <span className="text-xs text-muted shrink-0">至</span>
                  <input
                    type="date" value={bfEnd} onChange={e => setBfEnd(e.target.value)}
                    className="h-9 flex-1 min-w-0 rounded-lg border border-border bg-base px-2.5 text-xs font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25"
                  />
                  <button
                    onClick={handleBackfill}
                    disabled={bfRunning || !dateParam.trim() || !bfStart || !bfEnd}
                    title="按本地交易日逐日拉取写入历史分区; 已有分区自动跳过, 可重复执行"
                    className="h-9 shrink-0 inline-flex items-center gap-1.5 px-4 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent/90 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                  >
                    {bfRunning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <History className="h-3.5 w-3.5" />}
                    回补
                  </button>
                </div>
                <div className="text-[10px] text-muted/70">
                  单次上限 120 天（数秒至数分钟，取决于接口）；已有分区自动跳过，接口无该日数据记为空，可重复执行。
                </div>
                {bfResult && (
                  <div className="pt-2 border-t border-border/40 space-y-1">
                    <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-secondary">
                      <span>交易日 {bfResult.total_days}</span>
                      <span className="text-emerald-500">写入 {bfResult.fetched} 日 / {bfResult.rows_written} 行</span>
                      <span>跳过已有 {bfResult.skipped_existing}</span>
                      <span>无数据 {bfResult.empty}</span>
                      {bfResult.failed.length > 0 && <span className="text-danger">失败 {bfResult.failed.length}</span>}
                    </div>
                    {bfResult.failed.length > 0 && (
                      <div className="max-h-28 overflow-y-auto rounded-lg bg-base px-2 py-1.5 space-y-0.5">
                        {bfResult.failed.map(f => (
                          <div key={f.date} className="text-[9px] text-danger/90 font-mono">
                            {f.date} · {f.reason}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </section>
          )}

          {runResult && (
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/[0.06] p-3 flex items-center justify-between text-[11px]">
              <span className="text-emerald-500 font-medium flex items-center gap-1">
                <CheckCircle2 className="h-3.5 w-3.5" />拉取成功
              </span>
              <span className="text-secondary">{runResult.rows} 行 · {runResult.date}</span>
            </div>
          )}

          {testResult && (
            <div className="rounded-lg border border-accent/30 bg-accent/[0.04] p-3 space-y-1.5">
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-accent font-medium">测试成功</span>
                <span className="text-secondary">{testResult.total_rows} 行</span>
              </div>
              {!testResult.has_symbol && (
                <div className="text-[11px] text-amber-500">数据缺少 symbol 字段，请配置字段映射</div>
              )}
              {testResult.preview.length > 0 && (
                <pre className="text-[9px] font-mono text-muted bg-elevated rounded px-2 py-1.5 overflow-x-auto max-h-32">
                  {JSON.stringify(testResult.preview, null, 2)}
                </pre>
              )}
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2.5">
              <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 px-6 py-4 border-t border-border/50 bg-elevated/30">
          <button
            onClick={handleTest}
            disabled={testing || !url}
            className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg border border-border bg-elevated text-xs text-foreground hover:bg-border/30 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
          >
            {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
            测试
          </button>
          <button
            onClick={handleRun}
            disabled={running || !url}
            className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg bg-accent/15 border border-accent/25 text-accent text-xs font-medium hover:bg-accent/20 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
            立即执行
          </button>
          <div className="flex-1" />
          <button
            onClick={() => handleSave(false)}
            disabled={saving || !url}
            className="inline-flex items-center gap-1.5 h-9 px-5 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent/90 active:translate-y-px disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-all shadow-sm shadow-accent/20"
          >
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            保存配置
          </button>
        </div>
      </motion.div>
    </div>
  )
}
