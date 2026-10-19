import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import {
  X,
  Loader2,
  Upload,
  Plus,
  AlertCircle,
  Tag,
  Clock,
  Link2,
  FileText,
  Keyboard,
  RefreshCw,
  Database,
  Check,
} from 'lucide-react'
import { api, type ExtDataDetectUrlResult, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { SectionHead, INPUT_CLS, INPUT_SM_CLS } from './dialogShared'

type SourceMode = 'url' | 'file' | 'manual'

type MappingChoice = {
  fields: { name: string; dtype: string; label: string }[]
  need: 'symbol' | 'code' | 'both'
}

const WEEKDAY_OPTS: [number, string][] = [
  [1, '周一'], [2, '周二'], [3, '周三'], [4, '周四'], [5, '周五'], [6, '周六'], [7, '周日'],
]
const MONTHDAY_OPTS: [number, string][] = Array.from({ length: 31 }, (_, i) => [i + 1, `${i + 1} 号`])

export function CreateExtDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)
  const [sourceMode, setSourceMode] = useState<SourceMode>('url')
  const [id, setId] = useState('')
  const [label, setLabel] = useState('')
  const [description, setDescription] = useState('')
  const [mode, setMode] = useState<'snapshot' | 'timeseries'>('snapshot')
  // 市场级表: 行 = 全市场每日一条, 无标的列 (市场环境/情绪指数类), 跳过标的关联
  const [marketLevel, setMarketLevel] = useState(false)
  const [fields, setFields] = useState<ExtDataField[]>([])
  const [detectedSourceNames, setDetectedSourceNames] = useState<string[]>([])
  const [error, setError] = useState('')
  const detectFileRef = useRef<HTMLInputElement>(null)
  const [detecting, setDetecting] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [symbolMap, setSymbolMap] = useState<Record<string, string>>({})
  const [codeMap, setCodeMap] = useState<Record<string, string>>({})
  const [matchStatus, setMatchStatus] = useState<'none' | 'partial' | 'full'>('none')
  const [selectMapping, setSelectMapping] = useState<MappingChoice | null>(null)

  const [url, setUrl] = useState('')
  const [method, setMethod] = useState<'GET' | 'POST'>('GET')
  const [headerStr, setHeaderStr] = useState('')
  const [body, setBody] = useState('')
  const [responsePath, setResponsePath] = useState('')
  const [fieldMapStr, setFieldMapStr] = useState('')
  const [schedule, setSchedule] = useState(1440)
  // 调度二选一 (UI 态): 间隔制 / 固定时刻制, 显式切换; 切回间隔制清空时刻
  const [scheduleMode, setScheduleMode] = useState<'interval' | 'fixed'>('interval')
  const [scheduleTime, setScheduleTime] = useState('')
  const [schedulePeriod, setSchedulePeriod] = useState<'daily' | 'weekly' | 'monthly'>('daily')
  const [scheduleDay, setScheduleDay] = useState(1)
  const [scheduleTradingOnly, setScheduleTradingOnly] = useState(false)
  const [timeoutSec, setTimeoutSec] = useState(30)
  const [savePull, setSavePull] = useState(true)
  const [importNow, setImportNow] = useState(true)
  const [enablePull, setEnablePull] = useState(false)
  const [urlPreview, setUrlPreview] = useState<ExtDataDetectUrlResult | null>(null)

  const userFields = fields.filter(f => f.name !== 'symbol' && f.name !== 'code')

  const parseJsonObject = (str: string, labelText: string): Record<string, string> | undefined => {
    if (!str.trim()) return undefined
    try {
      const parsed = JSON.parse(str)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('not object')
      }
      return Object.fromEntries(
        Object.entries(parsed).map(([k, v]) => [k, String(v)]),
      )
    } catch {
      throw new Error(`${labelText} 不是有效 JSON 对象`)
    }
  }

  const buildPullFieldMap = () => {
    const base = parseJsonObject(fieldMapStr, '字段映射') ?? {}
    const finalMap: Record<string, string> = { ...base }

    fields.forEach((field, index) => {
      const source = detectedSourceNames[index]
      const target = field.name.trim()
      if (!source || !target || source === target) return

      const upstreamKeys = Object.entries(base)
        .filter(([, mapped]) => mapped === source)
        .map(([raw]) => raw)

      if (upstreamKeys.length) {
        upstreamKeys.forEach((raw) => { finalMap[raw] = target })
      } else {
        finalMap[source] = target
      }
    })

    return finalMap
  }

  const mapSubmitCol = (map: Record<string, string>, fieldMap: Record<string, string>) => {
    if (sourceMode !== 'url' || map.type !== 'mapped' || !map.col) return map
    const mappedCol = fieldMap[map.col] ?? map.col
    return { ...map, col: mappedCol }
  }

  const create = useMutation({
    mutationFn: async () => {
      if (scheduleMode === 'fixed' && !scheduleTime) {
        throw new Error('固定时刻模式请先选择时刻（或切回「按间隔」）')
      }
      const userF = fields.filter((f) => f.name.trim() && f.name !== 'symbol' && f.name !== 'code')
      const pullFieldMap = sourceMode === 'url' ? buildPullFieldMap() : {}
      const submittedSymbolMap = mapSubmitCol(symbolMap, pullFieldMap)
      const submittedCodeMap = mapSubmitCol(codeMap, pullFieldMap)

      const config = await api.extDataCreate({
        id,
        label,
        mode,
        fields: marketLevel
          ? userF
          : [
            { name: 'symbol', dtype: 'string', label: '标的代码' },
            { name: 'code', dtype: 'string', label: '代码' },
            ...userF,
          ],
        description: description.trim() || undefined,
        symbol_map: marketLevel ? {} : submittedSymbolMap,
        code_map: marketLevel ? {} : submittedCodeMap,
        market_level: marketLevel,
      })

      if (sourceMode === 'url' && (savePull || importNow || enablePull)) {
        const headers = parseJsonObject(headerStr, 'Headers')
        const finalFieldMap = Object.keys(pullFieldMap).length ? pullFieldMap : undefined
        await api.extDataPullConfig(config.id, {
          url: url.trim(),
          method,
          headers,
          body: method === 'POST' && body.trim() ? body : undefined,
          response_path: responsePath.trim() || urlPreview?.response_path || '',
          field_map: finalFieldMap,
          schedule_minutes: schedule,
          schedule_time: scheduleMode === 'fixed' ? scheduleTime : null,
          schedule_period: scheduleMode === 'fixed' ? schedulePeriod : null,
          schedule_day: scheduleMode === 'fixed' && schedulePeriod !== 'daily' ? scheduleDay : null,
          schedule_trading_only: scheduleMode === 'fixed' ? scheduleTradingOnly : false,
          enabled: enablePull,
          timeout_seconds: Number.isFinite(timeoutSec) && timeoutSec >= 5 && timeoutSec <= 300 ? timeoutSec : 30,
        })

        if (importNow) {
          await api.extDataPullRun(config.id)
        }
      }

      return config
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.extData })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      onClose()
    },
    onError: (err) => setError(err instanceof Error ? err.message : String(err)),
  })

  const addField = () => {
    setFields([...fields, { name: '', dtype: 'string', label: '' }])
    setDetectedSourceNames([...detectedSourceNames, ''])
  }

  const removeField = (i: number) => {
    setFields(fields.filter((_, idx) => idx !== i))
    setDetectedSourceNames(detectedSourceNames.filter((_, idx) => idx !== i))
  }

  const updateField = (i: number, key: keyof ExtDataField, val: string) => {
    setFields(fields.map((f, idx) => (idx === i ? { ...f, [key]: val } : f)))
  }

  const valid = Boolean(
    id.trim()
      && label.trim()
      && fields.some((f) => f.name.trim())
      && (marketLevel || matchStatus !== 'none')
      && (sourceMode !== 'url' || url.trim()),
  )

  const processDetection = (
    detected: { name: string; dtype: string; label: string }[],
    symCands: string[],
    codeCands: string[],
  ) => {
    let sm: Record<string, string> = {}
    let cm: Record<string, string> = {}
    let status: 'none' | 'partial' | 'full' = 'none'

    setDetectedSourceNames(detected.map(f => f.name))

    // 市场级表: 不做标的识别, 直接采认全部检测字段
    if (marketLevel) {
      setFields(detected)
      setSymbolMap({})
      setCodeMap({})
      setMatchStatus('full')
      return
    }

    if (symCands.length === 1 && codeCands.length === 1) {
      sm = { type: 'mapped', col: symCands[0] }
      cm = { type: 'mapped', col: codeCands[0] }
      status = 'full'
    } else if (symCands.length === 1 && codeCands.length === 0) {
      sm = { type: 'mapped', col: symCands[0] }
      cm = { type: 'computed', from: 'symbol', method: 'strip_exchange' }
      status = 'full'
    } else if (symCands.length === 0 && codeCands.length === 1) {
      cm = { type: 'mapped', col: codeCands[0] }
      sm = { type: 'computed', from: 'code', method: 'append_exchange' }
      status = 'full'
    } else if (symCands.length > 1 || codeCands.length > 1) {
      setSelectMapping({
        fields: detected,
        need: symCands.length > 0 && codeCands.length > 0 ? 'both' : symCands.length > 0 ? 'symbol' : 'code',
      })
      setFields(detected)
      return
    } else {
      setSelectMapping({ fields: detected, need: 'both' })
      setFields(detected)
      return
    }

    setSymbolMap(sm)
    setCodeMap(cm)
    setMatchStatus(status)
    setFields(detected)
    setSelectMapping(null)
  }

  const detectFile = (file: File) => {
    setSourceMode('file')
    setDetecting(true)
    setError('')
    setSelectMapping(null)
    setUrlPreview(null)
    api.extDataDetectFields(file)
      .then((res) => {
        processDetection(res.fields, res.symbol_candidates, res.code_candidates)
      })
      .catch((err) => setError(String(err)))
      .finally(() => setDetecting(false))
  }

  const handleDetectFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    e.target.value = ''
    detectFile(file)
  }

  const handleDetectUrl = () => {
    if (!url.trim()) {
      setError('请先填写 URL')
      return
    }
    setDetecting(true)
    setError('')
    setSelectMapping(null)
    setUrlPreview(null)

    let headers: Record<string, string> | undefined
    let fieldMap: Record<string, string> | undefined
    try {
      headers = parseJsonObject(headerStr, 'Headers')
      fieldMap = parseJsonObject(fieldMapStr, '字段映射')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setDetecting(false)
      return
    }

    api.extDataDetectUrl({
      url: url.trim(),
      method,
      headers,
      body: method === 'POST' && body.trim() ? body : undefined,
      response_path: responsePath.trim() || undefined,
      field_map: fieldMap,
      timeout_seconds: Number.isFinite(timeoutSec) && timeoutSec >= 5 && timeoutSec <= 300 ? timeoutSec : undefined,
    })
      .then((res) => {
        setUrlPreview(res)
        setResponsePath(res.response_path || responsePath)
        processDetection(res.fields, res.symbol_candidates, res.code_candidates)
      })
      .catch((err) => setError(String(err)))
      .finally(() => setDetecting(false))
  }

  const selectSource = (next: SourceMode) => {
    if (next !== sourceMode) {
      setFields([])
      setDetectedSourceNames([])
      setSymbolMap({})
      setCodeMap({})
      setMatchStatus('none')
      setSelectMapping(null)
      setUrlPreview(null)
    }
    setSourceMode(next)
    setError('')
    if (next === 'manual') {
      setSymbolMap({ type: 'mapped', col: 'symbol' })
      setCodeMap({ type: 'computed', from: 'symbol', method: 'strip_exchange' })
      setMatchStatus('full')
    }
  }

  const applyManualMapping = (fieldName: string) => {
    const sm = { type: 'mapped', col: fieldName }
    const cm = { type: 'computed', from: 'symbol', method: 'strip_exchange' }
    setSymbolMap(sm)
    setCodeMap(cm)
    setMatchStatus('full')
    setSelectMapping(null)
  }

  const previewColumns = urlPreview?.preview?.[0]
    ? Object.keys(urlPreview.preview[0]).slice(0, 5)
    : []

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" {...backdrop} />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative rounded-2xl border border-border bg-surface shadow-2xl mx-4 w-full max-w-3xl max-h-[88vh] flex flex-col overflow-hidden"
      >
        <div className="px-6 pt-5 pb-4 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="mt-0.5 p-2 rounded-xl bg-amber-500/10 text-amber-500 border border-amber-500/20 shrink-0">
              <Database className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <h3 className="text-[15px] leading-6 font-semibold text-foreground">新增扩展数据</h3>
              <p className="text-[11px] text-muted mt-0.5 leading-relaxed [text-wrap:balance]">
                接入第三方接口或本地文件，自动识别字段并与标的关联 — 适用于概念、人气、资金流、评分标签等场景
              </p>
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
          {/* 01 接入方式 */}
          <section>
            <SectionHead num="01" title="接入方式" />
            <div className="grid grid-cols-3 gap-1.5 rounded-xl bg-elevated/40 p-1 border border-border/40">
              {([
                ['url', '接口 URL', Link2],
                ['file', '数据文件', FileText],
                ['manual', '手动定义', Keyboard],
              ] as const).map(([key, text, Icon]) => {
                const active = sourceMode === key
                return (
                  <button
                    key={key}
                    onClick={() => selectSource(key)}
                    className={`inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium transition-all duration-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 ${
                      active
                        ? 'bg-surface text-foreground shadow-sm'
                        : 'text-muted hover:text-secondary hover:bg-elevated/60'
                    }`}
                  >
                    <Icon className={`h-3.5 w-3.5 transition-colors ${active ? 'text-amber-500' : ''}`} />
                    {text}
                  </button>
                )
              })}
            </div>
          </section>

          {/* 02 数据类型 */}
          <section>
            <SectionHead num="02" title="数据类型" />
            <div className="grid grid-cols-2 gap-2.5">
              {(['snapshot', 'timeseries'] as const).map((m) => {
                const active = mode === m
                return (
                  <button
                    key={m}
                    onClick={() => setMode(m)}
                    className={`relative flex items-start gap-3 px-4 py-3.5 rounded-xl border text-left transition-all duration-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-amber-500/40 ${
                      active
                        ? 'border-amber-500/40 bg-amber-500/[0.07] shadow-sm shadow-amber-500/10'
                        : 'border-border bg-elevated/30 hover:bg-elevated/60 hover:border-border'
                    }`}
                  >
                    <div className={`mt-0.5 p-1.5 rounded-lg transition-colors ${
                      active ? 'bg-amber-500/15 text-amber-500' : 'bg-elevated text-muted'
                    }`}>
                      {m === 'snapshot' ? <Tag className="h-4 w-4" /> : <Clock className="h-4 w-4" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className={`text-xs font-medium ${active ? 'text-foreground' : 'text-secondary'}`}>
                        {m === 'snapshot' ? '快照型' : '时序型'}
                        <span className="ml-1.5 text-[10px] font-normal text-muted">{m === 'snapshot' ? '每股一条' : '按日分区'}</span>
                      </div>
                      <div className="text-[10px] text-muted mt-1 leading-relaxed">
                        {m === 'snapshot' ? '每个标的一条最新值，如概念、行业、人气排名' : '按日期记录历史，如资金流、情绪指数、竞价序列'}
                      </div>
                    </div>
                    {active && (
                      <span className="mt-1 inline-flex h-4 w-4 items-center justify-center rounded-full bg-amber-500 text-white shrink-0">
                        <Check className="h-2.5 w-2.5" strokeWidth={3} />
                      </span>
                    )}
                  </button>
                )
              })}
            </div>

            {/* 市场级: 行=全市场一条, 无标的列 (择时状态/情绪指数类) */}
            <label className="mt-2.5 flex cursor-pointer items-center gap-2.5 rounded-lg border border-border/50 bg-elevated/30 px-3 py-2 hover:bg-elevated/50 transition-colors">
              <input
                type="checkbox"
                checked={marketLevel}
                onChange={e => {
                  setMarketLevel(e.target.checked)
                  if (e.target.checked) {
                    setSymbolMap({})
                    setCodeMap({})
                    setMatchStatus('full')
                  } else {
                    setMatchStatus('none')
                  }
                }}
                className="h-3.5 w-3.5 shrink-0 accent-amber-500"
              />
              <span className="text-[11px] text-secondary">
                市场级数据
                <span className="ml-1.5 text-muted" title="行 = 全市场每日一条, 无标的代码列。适用于市场环境/情绪指数/择时状态等序列, 跳过标的关联; 通过 /rows 与 /values 接口消费">
                  每行是全市场一条（无标的列），跳过标的关联
                </span>
              </span>
            </label>
          </section>

          {/* 03 基本信息 */}
          <section>
            <SectionHead num="03" title="基本信息" />
            <div className="grid grid-cols-2 gap-3">
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">显示名称</div>
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder={mode === 'snapshot' ? '例: 概念' : '例: 资金流'}
                  className={INPUT_CLS}
                />
              </div>
              <div>
                <div className="text-[11px] font-medium text-secondary mb-1.5">
                  标识符
                  <span className="ml-1.5 text-[10px] font-normal text-muted">建表后不可改</span>
                </div>
                <input
                  value={id}
                  onChange={(e) => setId(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))}
                  placeholder={mode === 'snapshot' ? '例: concept' : '例: money_flow'}
                  className={`${INPUT_CLS} font-mono`}
                />
              </div>
            </div>
            <div className="mt-3">
              <div className="text-[11px] font-medium text-secondary mb-1.5">描述 <span className="text-muted font-normal">可选</span></div>
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="一句话说明数据用途，展示在表卡片上"
                className={INPUT_CLS}
              />
            </div>
          </section>

          {/* URL 请求配置 */}
          {sourceMode === 'url' && (
            <section className="rounded-xl border border-border/60 bg-elevated/20 p-4 space-y-3">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
                <Link2 className="h-3.5 w-3.5 text-amber-500" />
                <span>接口请求</span>
                <span className="ml-1 text-[10px] font-normal text-muted">先测试识别，系统按返回数据生成字段</span>
              </div>
              <div className="flex gap-2">
                <select
                  value={method}
                  onChange={(e) => setMethod(e.target.value as 'GET' | 'POST')}
                  className="h-9 shrink-0 rounded-lg border border-border bg-base px-2.5 text-xs font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50"
                >
                  <option value="GET">GET</option>
                  <option value="POST">POST</option>
                </select>
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://api.example.com/data"
                  className="h-9 flex-1 min-w-0 rounded-lg border border-border bg-base px-3 text-xs font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                />
                <button
                  onClick={handleDetectUrl}
                  disabled={detecting || !url.trim()}
                  className="h-9 inline-flex items-center gap-1.5 rounded-lg bg-accent px-3.5 text-xs font-medium text-white hover:bg-accent/90 active:translate-y-px disabled:opacity-40 disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-all"
                >
                  {detecting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                  测试识别
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <div className="text-[10px] text-muted mb-1">响应数据路径</div>
                  <input
                    value={responsePath}
                    onChange={(e) => setResponsePath(e.target.value)}
                    placeholder="data.list · 可自动识别"
                    className={`${INPUT_SM_CLS} font-mono`}
                  />
                </div>
                <div>
                  <div className="text-[10px] text-muted mb-1">拉取超时（秒）</div>
                  <input
                    type="number" min={5} max={300} step={5}
                    value={timeoutSec}
                    onChange={(e) => setTimeoutSec(Number(e.target.value))}
                    title="测试识别与正式拉取的单次请求超时, 默认 30 秒, 范围 5~300"
                    className={`${INPUT_SM_CLS} font-mono tabular-nums`}
                  />
                </div>
              </div>
              <div className="rounded-lg border border-border/50 bg-base/40 p-2.5 space-y-2">
                <div className="text-[10px] text-muted">调度方式</div>
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
                  <div>
                    <div className="text-[10px] text-muted/70 mb-1">间隔（分钟）</div>
                    <input
                      type="number"
                      min={1}
                      value={schedule}
                      onChange={(e) => setSchedule(Number(e.target.value) || 1)}
                      className={`${INPUT_SM_CLS} font-mono tabular-nums`}
                    />
                    <div className="text-[9px] text-muted/70 mt-1">
                      每隔 N 分钟拉取一次（从启用时刻起算）—「每天 09:30」这类日程请用固定时刻
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="grid grid-cols-3 gap-2">
                      <select
                        value={schedulePeriod}
                        onChange={(e) => setSchedulePeriod(e.target.value as 'daily' | 'weekly' | 'monthly')}
                        className="h-8 rounded-lg border border-border bg-base px-2 text-[11px] text-foreground cursor-pointer focus:outline-none focus:ring-1 focus:ring-accent/25"
                      >
                        <option value="daily">每日</option>
                        <option value="weekly">每周</option>
                        <option value="monthly">每月</option>
                      </select>
                      <select
                        value={String(scheduleDay)}
                        onChange={(e) => setScheduleDay(Number(e.target.value))}
                        disabled={schedulePeriod === 'daily'}
                        title={schedulePeriod === 'weekly' ? '每周几拉取' : '每月几号拉取 (短月无该日则该月跳过)'}
                        className="h-8 rounded-lg border border-border bg-base px-2 text-[11px] text-foreground cursor-pointer focus:outline-none focus:ring-1 focus:ring-accent/25 disabled:opacity-40"
                      >
                        {(schedulePeriod === 'weekly' ? WEEKDAY_OPTS : MONTHDAY_OPTS).map(([v, t]) => (
                          <option key={v} value={String(v)}>{t}</option>
                        ))}
                      </select>
                      <div className="flex items-center gap-1.5">
                        <input
                          type="time"
                          value={scheduleTime}
                          onChange={(e) => setScheduleTime(e.target.value)}
                          title="如 09:30 — 周期日该时刻拉取一次; 启用时若当天已过该时刻且未拉过会立即补拉一次"
                          className={`${INPUT_SM_CLS} flex-1 min-w-0 font-mono`}
                        />
                        {scheduleTime && (
                          <button
                            type="button"
                            onClick={() => setScheduleTime('')}
                            className="h-8 shrink-0 px-2.5 rounded-lg border border-border bg-base text-[10px] text-muted hover:text-foreground hover:bg-elevated focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                          >
                            清除
                          </button>
                        )}
                      </div>
                    </div>
                    <label className="flex items-center gap-1.5 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={scheduleTradingOnly}
                        onChange={(e) => setScheduleTradingOnly(e.target.checked)}
                        className="h-3.5 w-3.5 shrink-0 accent-amber-500"
                      />
                      <span className="text-[10px] text-secondary">
                        仅交易日 — 跳过周六日
                        <span className="text-muted/70">（节假日无法离线判定，仍会按期尝试）</span>
                      </span>
                    </label>
                  </>
                )}
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <div className="text-[10px] text-muted mb-1">Headers（JSON，可选）</div>
                  <textarea
                    value={headerStr}
                    onChange={(e) => setHeaderStr(e.target.value)}
                    rows={2}
                    placeholder='{"Authorization":"Bearer xxx"}'
                    className="w-full rounded-lg border border-border bg-base px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                  />
                </div>
                <div>
                  <div className="text-[10px] text-muted mb-1">字段映射（外部名 → 内部名，可选）</div>
                  <textarea
                    value={fieldMapStr}
                    onChange={(e) => setFieldMapStr(e.target.value)}
                    rows={2}
                    placeholder='{"code":"symbol","val":"score"}'
                    className="w-full rounded-lg border border-border bg-base px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                  />
                </div>
              </div>
              {method === 'POST' && (
                <div>
                  <div className="text-[10px] text-muted mb-1">请求体（JSON，可选）</div>
                  <textarea
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    rows={3}
                    placeholder='{"page":1}'
                    className="w-full rounded-lg border border-border bg-base px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                  />
                </div>
              )}
              {urlPreview && (
                <div className="rounded-lg border border-border/50 bg-base/60 p-3 space-y-2">
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-secondary tabular-nums">
                      识别到 <span className="text-foreground font-medium">{urlPreview.total_rows}</span> 行 · 路径{' '}
                      <span className="font-mono">{urlPreview.response_path || '根数组'}</span>
                    </span>
                    {urlPreview.response_path_candidates.length > 1 && (
                      <span className="text-muted">候选路径 {urlPreview.response_path_candidates.length} 个</span>
                    )}
                  </div>
                  {previewColumns.length > 0 && (
                    <div className="overflow-x-auto rounded-md border border-border/40">
                      <table className="min-w-full text-left text-[10px]">
                        <thead className="bg-elevated/50 text-muted">
                          <tr>
                            {previewColumns.map(col => <th key={col} className="px-2 py-1 font-medium font-mono">{col}</th>)}
                          </tr>
                        </thead>
                        <tbody>
                          {urlPreview.preview.slice(0, 3).map((row, idx) => (
                            <tr key={idx} className="border-t border-border/30 text-secondary">
                              {previewColumns.map(col => (
                                <td key={col} className="max-w-[140px] truncate px-2 py-1 font-mono">
                                  {row[col] == null ? '—' : String(row[col])}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </section>
          )}

          {/* 04 字段定义 */}
          <section>
            <SectionHead num="04" title="字段定义">
              {(sourceMode === 'manual' || fields.length > 0) && (
                <div className="flex items-center gap-1.5">
                  {sourceMode !== 'url' && (
                    <button
                      onClick={() => detectFileRef.current?.click()}
                      disabled={detecting}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] text-muted hover:text-accent hover:bg-accent/[0.06] disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                    >
                      {detecting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
                      从数据文件识别
                    </button>
                  )}
                  <button
                    onClick={addField}
                    className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-md text-[10px] text-muted hover:text-accent hover:bg-accent/[0.06] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                  >
                    <Plus className="h-3 w-3" />
                    添加字段
                  </button>
                </div>
              )}
            </SectionHead>

            <input
              ref={detectFileRef}
              type="file"
              accept=".csv,.xlsx,.xls"
              className="hidden"
              onChange={handleDetectFile}
            />

            {selectMapping && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="mb-2 rounded-lg border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2.5 space-y-2"
              >
                <div className="text-[10px] text-amber-500 font-medium">
                  未自动识别到唯一标的代码，请选择哪一列作为标的代码
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {selectMapping.fields.map(f => (
                    <button
                      key={f.name}
                      onClick={() => applyManualMapping(f.name)}
                      className="px-2.5 py-1 rounded-md bg-accent/10 text-accent text-[10px] font-medium hover:bg-accent/20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                    >
                      {f.name}
                    </button>
                  ))}
                  <button
                    onClick={() => setSelectMapping(null)}
                    className="px-2.5 py-1 rounded-md bg-elevated text-muted text-[10px] hover:text-secondary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
                  >
                    取消
                  </button>
                </div>
              </motion.div>
            )}

            {sourceMode === 'file' && fields.length === 0 ? (
              <div
                onClick={() => detectFileRef.current?.click()}
                onDragOver={e => { e.preventDefault(); setDragOver(true) }}
                onDragLeave={() => setDragOver(false)}
                onDrop={e => {
                  e.preventDefault()
                  setDragOver(false)
                  const file = e.dataTransfer.files[0]
                  if (file) detectFile(file)
                }}
                className={`rounded-xl border-2 border-dashed py-8 flex flex-col items-center justify-center gap-2 cursor-pointer transition-all duration-200 ${
                  dragOver
                    ? 'border-amber-500 bg-amber-500/[0.06] scale-[1.01]'
                    : detecting
                      ? 'border-border/40 pointer-events-none'
                      : 'border-border/40 hover:border-amber-500/40 hover:bg-amber-500/[0.02]'
                }`}
              >
                {detecting ? (
                  <><Loader2 className="h-5 w-5 text-amber-500 animate-spin" /><span className="text-[11px] text-muted">检测字段中…</span></>
                ) : (
                  <>
                    <div className="p-2.5 rounded-xl bg-elevated/60 text-muted/70">
                      <Upload className="h-5 w-5" />
                    </div>
                    <span className="text-[11px] text-secondary">拖拽或点击上传数据文件（CSV / Excel）</span>
                    <span className="text-[10px] text-muted/70">自动识别列名和类型，symbol / code 列自动匹配</span>
                  </>
                )}
              </div>
            ) : sourceMode === 'url' && fields.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border/40 bg-elevated/20 py-6 flex flex-col items-center gap-1.5">
                <RefreshCw className="h-4 w-4 text-muted/50" />
                <span className="text-[11px] text-muted">填写 URL 后点击「测试识别」，按返回数据自动生成字段</span>
              </div>
            ) : sourceMode === 'manual' && fields.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border/40 bg-elevated/20 py-6 flex flex-col items-center gap-2.5">
                <span className="text-[11px] text-muted">手动添加字段后即可创建空表结构</span>
                <button
                  onClick={addField}
                  className="inline-flex items-center gap-1 rounded-lg bg-accent px-3.5 py-1.5 text-xs font-medium text-white hover:bg-accent/90 active:translate-y-px focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-all"
                >
                  <Plus className="h-3.5 w-3.5" />
                  添加字段
                </button>
              </div>
            ) : (
              <div className="space-y-1">
                {[
                  { name: '标的代码', key: 'symbol', mapped: matchStatus !== 'none', hint: symbolMap.type === 'mapped' ? `← ${symbolMap.col}` : '← 计算' },
                  { name: '代码', key: 'code', mapped: matchStatus !== 'none', hint: codeMap.type === 'mapped' ? `← ${codeMap.col}` : codeMap.method === 'strip_exchange' ? '← symbol截取' : '← 推算' },
                ].map(row => (
                  <div
                    key={row.key}
                    className={`flex items-center gap-2.5 px-3 py-2 rounded-lg border ${
                      row.mapped ? 'border-border/40 bg-elevated/20' : 'border-danger/30 bg-danger/[0.04]'
                    }`}
                  >
                    <span className="w-16 shrink-0 text-[11px] text-secondary">{row.name}</span>
                    <span className="flex-1 text-[11px] font-mono text-muted">{row.key}</span>
                    <span className="w-10 text-center text-[10px] text-muted/50">文本</span>
                    {row.mapped
                      ? <span className="inline-flex items-center gap-1 text-[10px] text-green-600 dark:text-green-500/80 shrink-0">
                          <Check className="h-3 w-3" strokeWidth={2.5} />
                          {row.hint}
                        </span>
                      : <AlertCircle className="h-3.5 w-3.5 text-danger/60 shrink-0" />}
                  </div>
                ))}
                {userFields.map((f) => {
                  const idx = fields.indexOf(f)
                  return (
                    <div key={idx} className="flex items-center gap-2 group">
                      <input
                        value={f.label}
                        onChange={(e) => updateField(idx, 'label', e.target.value)}
                        placeholder="显示名"
                        className="w-24 h-8 px-2.5 rounded-lg border border-border bg-base text-[11px] text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                      />
                      <input
                        value={f.name}
                        onChange={(e) => updateField(idx, 'name', e.target.value)}
                        placeholder="字段名（英文）"
                        className="flex-1 h-8 px-2.5 rounded-lg border border-border bg-base text-[11px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50 transition-shadow"
                      />
                      <select
                        value={f.dtype}
                        onChange={(e) => updateField(idx, 'dtype', e.target.value)}
                        className="h-8 px-2 rounded-lg border border-border bg-base text-[11px] text-foreground focus:outline-none focus:ring-1 focus:ring-accent/25 focus:border-accent/50"
                      >
                        <option value="string">文本</option>
                        <option value="int">整数</option>
                        <option value="float">小数</option>
                        <option value="bool">布尔</option>
                      </select>
                      <button
                        onClick={() => removeField(idx)}
                        aria-label="删除字段"
                        className="p-1.5 rounded-md text-muted/40 hover:text-danger hover:bg-danger/10 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-danger/40 transition-all"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          {/* 创建选项 */}
          {sourceMode === 'url' && (
            <section className="rounded-xl border border-border/60 bg-elevated/20 p-4 space-y-1">
              <div className="text-xs font-semibold text-foreground mb-1.5">创建选项</div>
              {([
                { checked: savePull, set: (v: boolean) => { setSavePull(v); if (!v) { setImportNow(false); setEnablePull(false) } }, label: '保存为拉取配置', hint: '记录请求参数，之后可在表卡片中调整', disabled: false },
                { checked: importNow, set: (v: boolean) => { setImportNow(v); if (v) setSavePull(true) }, label: '创建后立即导入本次数据', hint: '立刻执行一次拉取并写入 Parquet', disabled: !savePull },
                { checked: enablePull, set: (v: boolean) => { setEnablePull(v); if (v) setSavePull(true) }, label: '启用定时拉取', hint: scheduleMode === 'fixed' && scheduleTime
                  ? `${
                    schedulePeriod === 'weekly' ? `每周${WEEKDAY_OPTS[scheduleDay - 1][1].slice(1)}`
                      : schedulePeriod === 'monthly' ? `每月 ${scheduleDay} 号`
                        : '每天'
                    } ${scheduleTime}${scheduleTradingOnly ? '（仅交易日）' : ''}`
                  : `每 ${schedule} 分钟自动拉取`, disabled: !savePull },
              ] as const).map(opt => (
                <label
                  key={opt.label}
                  className={`flex items-start gap-2.5 px-2 py-1.5 -mx-2 rounded-lg transition-colors ${opt.disabled ? 'opacity-40 pointer-events-none' : 'hover:bg-elevated/50 cursor-pointer'}`}
                >
                  <input
                    type="checkbox"
                    checked={opt.checked}
                    onChange={e => opt.set(e.target.checked)}
                    className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-accent"
                  />
                  <span className="text-[11px] text-secondary leading-relaxed">
                    {opt.label}
                    <span className="block text-[10px] text-muted/80">{opt.hint}</span>
                  </span>
                </label>
              ))}
            </section>
          )}

          {error && (
            <div className="flex items-start gap-2 text-[11px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-3 py-2.5">
              <AlertCircle className="h-3.5 w-3.5 mt-px shrink-0" />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-4 px-6 py-4 border-t border-border/50 bg-elevated/30">
          <div className="text-[10px] text-muted/70 leading-relaxed">
            {sourceMode === 'url'
              ? '创建后可在扩展数据卡片中继续调整拉取配置'
              : sourceMode === 'file'
                ? '文件识别用于生成表结构，创建后仍可上传文件写入数据'
                : '手动创建后可通过文件、推送或拉取写入数据'}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={onClose}
              className="px-4 py-2 rounded-lg bg-elevated text-secondary text-xs hover:bg-elevated/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-colors"
            >
              取消
            </button>
            <button
              onClick={() => create.mutate()}
              disabled={!valid || create.isPending}
              className="px-5 py-2 inline-flex items-center gap-1.5 rounded-lg bg-accent text-white text-xs font-medium hover:bg-accent/90 active:translate-y-px disabled:opacity-40 disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 transition-all shadow-sm shadow-accent/20"
            >
              {create.isPending
                ? <><Loader2 className="h-3.5 w-3.5 animate-spin" />创建中…</>
                : <><Plus className="h-3.5 w-3.5" />创建</>}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  )
}
