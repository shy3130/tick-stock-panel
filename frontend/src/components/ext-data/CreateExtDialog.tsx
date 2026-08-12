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
} from 'lucide-react'
import { api, type ExtDataDetectUrlResult, type ExtDataField } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'

type SourceMode = 'url' | 'file' | 'manual'

type MappingChoice = {
  fields: { name: string; dtype: string; label: string }[]
  need: 'symbol' | 'code' | 'both'
}

export function CreateExtDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const backdrop = useDialogBackdrop(onClose)
  const [sourceMode, setSourceMode] = useState<SourceMode>('url')
  const [id, setId] = useState('')
  const [label, setLabel] = useState('')
  const [description, setDescription] = useState('')
  const [mode, setMode] = useState<'snapshot' | 'timeseries'>('snapshot')
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
      const userF = fields.filter((f) => f.name.trim() && f.name !== 'symbol' && f.name !== 'code')
      const pullFieldMap = sourceMode === 'url' ? buildPullFieldMap() : {}
      const submittedSymbolMap = mapSubmitCol(symbolMap, pullFieldMap)
      const submittedCodeMap = mapSubmitCol(codeMap, pullFieldMap)

      const config = await api.extDataCreate({
        id,
        label,
        mode,
        fields: [
          { name: 'symbol', dtype: 'string', label: '标的代码' },
          { name: 'code', dtype: 'string', label: '代码' },
          ...userF,
        ],
        description: description.trim() || undefined,
        symbol_map: submittedSymbolMap,
        code_map: submittedCodeMap,
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
          enabled: enablePull,
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
      && matchStatus !== 'none'
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
        className="relative rounded-2xl border border-border bg-surface shadow-2xl mx-4 w-full max-w-2xl max-h-[88vh] flex flex-col overflow-hidden"
      >
        <div className="px-6 pt-5 pb-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-base font-semibold text-foreground">新增扩展数据</h3>
              <p className="text-[11px] mt-1 inline-flex items-center gap-1 bg-amber-500/10 text-amber-400 px-2 py-0.5 rounded-md font-medium">
                接入自有数据，与标的自动关联（第三方接口或 CSV/Excel），支持概念、人气、资金流、舆情、研报评分标签等场景
              </p>
            </div>
            <button onClick={onClose} className="p-1 rounded-lg hover:bg-elevated text-secondary transition-colors">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-6 pb-6 space-y-5">
          <div>
            <div className="text-[11px] font-medium text-secondary mb-2">接入方式</div>
            <div className="grid grid-cols-3 gap-2 rounded-xl bg-elevated/40 p-1">
              {([
                ['url', 'URL', Link2],
                ['file', '文件', FileText],
                ['manual', '手动', Keyboard],
              ] as const).map(([key, text, Icon]) => {
                const active = sourceMode === key
                return (
                  <button
                    key={key}
                    onClick={() => selectSource(key)}
                    className={`inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium transition-colors ${
                      active ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary'
                    }`}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {text}
                  </button>
                )
              })}
            </div>
          </div>

          <div>
            <div className="text-[11px] font-medium text-secondary mb-2">数据类型</div>
            <div className="grid grid-cols-2 gap-2">
              {(['snapshot', 'timeseries'] as const).map((m) => {
                const active = mode === m
                return (
                  <button
                    key={m}
                    onClick={() => setMode(m)}
                    className={`relative flex items-start gap-3 px-4 py-3 rounded-xl border transition-all duration-200 text-left ${
                      active
                        ? 'border-amber-500/40 bg-amber-500/[0.08] shadow-sm shadow-amber-500/10'
                        : 'border-border bg-elevated/30 hover:bg-elevated/60'
                    }`}
                  >
                    <div className={`mt-0.5 p-1.5 rounded-lg ${
                      active
                        ? 'bg-amber-500/15 text-amber-400'
                        : 'bg-elevated text-muted'
                    }`}>
                      {m === 'snapshot' ? <Tag className="h-4 w-4" /> : <Clock className="h-4 w-4" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className={`text-xs font-medium ${active ? 'text-foreground' : 'text-secondary'}`}>
                        {m === 'snapshot' ? '快照型' : '时序型'}
                      </div>
                      <div className="text-[10px] text-muted mt-0.5 leading-relaxed">
                        {m === 'snapshot' ? '每个标的一条，如概念、行业、人气排名' : '按日期记录，如资金流、情绪指数'}
                      </div>
                    </div>
                  </button>
                )
              })}
            </div>
          </div>

          <div className="grid grid-cols-[1fr_1fr] gap-3">
            <div>
              <div className="text-[11px] font-medium text-secondary mb-1.5">显示名称</div>
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder={mode === 'snapshot' ? '例: 概念' : '例: 资金流'}
                className="w-full h-9 px-3 rounded-lg bg-base border border-border text-xs text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/30 focus:border-accent/50 transition-shadow"
              />
            </div>
            <div>
              <div className="text-[11px] font-medium text-secondary mb-1.5">标识符</div>
              <input
                value={id}
                onChange={(e) => setId(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))}
                placeholder={mode === 'snapshot' ? '例: concept' : '例: money_flow'}
                className="w-full h-9 px-3 rounded-lg bg-base border border-border text-xs text-foreground font-mono placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/30 focus:border-accent/50 transition-shadow"
              />
            </div>
          </div>

          <div>
            <div className="text-[11px] font-medium text-secondary mb-1.5">描述</div>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="扩展数据 · 与标的 JOIN（可选自定义描述）"
              className="w-full h-9 px-3 rounded-lg bg-base border border-border text-xs text-foreground placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/30 focus:border-accent/50 transition-shadow"
            />
          </div>

          {sourceMode === 'url' && (
            <div className="rounded-xl border border-border/60 bg-elevated/20 p-3 space-y-3">
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
                <Link2 className="h-3.5 w-3.5 text-muted" />
                <span>URL 请求配置</span>
              </div>
              <div className="flex gap-2">
                <select
                  value={method}
                  onChange={(e) => setMethod(e.target.value as 'GET' | 'POST')}
                  className="h-8 shrink-0 rounded-lg border border-border bg-base px-2 text-[11px] text-foreground"
                >
                  <option value="GET">GET</option>
                  <option value="POST">POST</option>
                </select>
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://api.example.com/data"
                  className="h-8 flex-1 min-w-0 rounded-lg border border-border bg-base px-3 text-[11px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/50"
                />
                <button
                  onClick={handleDetectUrl}
                  disabled={detecting || !url.trim()}
                  className="h-8 inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-medium text-base hover:bg-accent/90 disabled:opacity-40 transition-colors"
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
                    placeholder="data.list（可留空自动识别）"
                    className="w-full h-8 rounded-lg border border-border bg-base px-2 text-[10px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/50"
                  />
                </div>
                <div>
                  <div className="text-[10px] text-muted mb-1">调度间隔（分钟）</div>
                  <input
                    type="number"
                    min={1}
                    value={schedule}
                    onChange={(e) => setSchedule(Number(e.target.value) || 1)}
                    className="w-full h-8 rounded-lg border border-border bg-base px-2 text-[10px] font-mono text-foreground focus:outline-none focus:border-accent/50"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <div className="text-[10px] text-muted mb-1">Headers（JSON，可选）</div>
                  <textarea
                    value={headerStr}
                    onChange={(e) => setHeaderStr(e.target.value)}
                    rows={2}
                    placeholder='{"Authorization":"Bearer xxx"}'
                    className="w-full rounded-lg border border-border bg-base px-2 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:border-accent/50"
                  />
                </div>
                <div>
                  <div className="text-[10px] text-muted mb-1">字段映射（外部名 → 内部名，可选）</div>
                  <textarea
                    value={fieldMapStr}
                    onChange={(e) => setFieldMapStr(e.target.value)}
                    rows={2}
                    placeholder='{"code":"symbol","val":"score"}'
                    className="w-full rounded-lg border border-border bg-base px-2 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:border-accent/50"
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
                    className="w-full rounded-lg border border-border bg-base px-2 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none focus:outline-none focus:border-accent/50"
                  />
                </div>
              )}
              {urlPreview && (
                <div className="rounded-lg border border-border/50 bg-base/60 p-2.5 space-y-2">
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-secondary">识别到 {urlPreview.total_rows} 行 · 路径 {urlPreview.response_path || '根数组'}</span>
                    {urlPreview.response_path_candidates.length > 1 && (
                      <span className="text-muted">候选路径 {urlPreview.response_path_candidates.length} 个</span>
                    )}
                  </div>
                  {previewColumns.length > 0 && (
                    <div className="overflow-x-auto rounded-md border border-border/40">
                      <table className="min-w-full text-left text-[10px]">
                        <thead className="bg-elevated/50 text-muted">
                          <tr>
                            {previewColumns.map(col => <th key={col} className="px-2 py-1 font-medium">{col}</th>)}
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
            </div>
          )}

          <div>
            <div className="flex items-center justify-between mb-2">
              <div className="text-[11px] font-medium text-secondary">字段定义</div>
              {(sourceMode === 'manual' || fields.length > 0) && (
                <div className="flex items-center gap-1.5">
                  {sourceMode !== 'url' && (
                    <button
                      onClick={() => detectFileRef.current?.click()}
                      disabled={detecting}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] text-muted hover:text-accent hover:bg-accent/[0.06] disabled:opacity-40 transition-colors"
                    >
                      {detecting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
                      从数据文件识别
                    </button>
                  )}
                  <button
                    onClick={addField}
                    className="inline-flex items-center gap-0.5 px-2 py-0.5 rounded-md text-[10px] text-muted hover:text-accent hover:bg-accent/[0.06] transition-colors"
                  >
                    <Plus className="h-3 w-3" />
                    添加字段
                  </button>
                </div>
              )}
            </div>

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
                <div className="text-[10px] text-amber-400 font-medium">
                  未自动识别到唯一标的代码，请选择哪一列作为标的代码
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {selectMapping.fields.map(f => (
                    <button
                      key={f.name}
                      onClick={() => applyManualMapping(f.name)}
                      className="px-2.5 py-1 rounded-md bg-accent/10 text-accent text-[10px] font-medium hover:bg-accent/20 transition-colors"
                    >
                      {f.name}
                    </button>
                  ))}
                  <button
                    onClick={() => setSelectMapping(null)}
                    className="px-2.5 py-1 rounded-md bg-elevated text-muted text-[10px] hover:text-secondary transition-colors"
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
                className={`rounded-xl border-2 border-dashed py-6 flex flex-col items-center justify-center gap-2 cursor-pointer transition-colors ${
                  dragOver ? 'border-accent bg-accent/[0.06]' : detecting ? 'border-border/40 pointer-events-none' : 'border-border/30 hover:border-accent/40 hover:bg-accent/[0.02]'
                }`}
              >
                {detecting ? (
                  <><Loader2 className="h-5 w-5 text-accent animate-spin" /><span className="text-[11px] text-muted">检测字段中…</span></>
                ) : (
                  <>
                    <Upload className="h-5 w-5 text-muted/60" />
                    <span className="text-[11px] text-secondary">上传数据文件（CSV / Excel）自动识别数据格式</span>
                    <span className="text-[10px] text-amber-400/70">自动识别列名和类型，symbol/code 列自动匹配</span>
                  </>
                )}
              </div>
            ) : sourceMode === 'url' && fields.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border/40 bg-elevated/20 py-5 text-center text-[11px] text-muted">
                填写 URL 后点击测试识别，系统会根据返回数据生成字段
              </div>
            ) : sourceMode === 'manual' && fields.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border/40 bg-elevated/20 py-5 flex flex-col items-center gap-2">
                <div className="text-[11px] text-muted">手动添加字段后即可创建空表结构</div>
                <button
                  onClick={addField}
                  className="inline-flex items-center gap-1 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-base hover:bg-accent/90 transition-colors"
                >
                  <Plus className="h-3.5 w-3.5" />
                  添加字段
                </button>
              </div>
            ) : (
              <div className="space-y-1">
                <div className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border ${matchStatus !== 'none' ? 'border-border/40 bg-elevated/20' : 'border-danger/30 bg-danger/[0.04]'}`}>
                  <span className="w-[72px] shrink-0 text-[11px] text-muted">标的代码</span>
                  <span className="flex-1 text-[11px] font-mono text-muted">symbol</span>
                  <span className="w-[52px] text-center text-[10px] text-muted/40">文本</span>
                  {matchStatus !== 'none'
                    ? <span className="text-[9px] text-green-500/70 shrink-0">
                        {symbolMap.type === 'mapped' ? `← ${symbolMap.col}` : '← 计算'}
                      </span>
                    : <AlertCircle className="h-3.5 w-3.5 text-danger/60 shrink-0" />}
                </div>
                <div className={`flex items-center gap-2 px-2.5 py-1.5 rounded-md border ${matchStatus !== 'none' ? 'border-border/40 bg-elevated/20' : 'border-danger/30 bg-danger/[0.04]'}`}>
                  <span className="w-[72px] shrink-0 text-[11px] text-muted">代码</span>
                  <span className="flex-1 text-[11px] font-mono text-muted">code</span>
                  <span className="w-[52px] text-center text-[10px] text-muted/40">文本</span>
                  {matchStatus !== 'none'
                    ? <span className="text-[9px] text-green-500/70 shrink-0">
                        {codeMap.type === 'mapped' ? `← ${codeMap.col}` : codeMap.method === 'strip_exchange' ? '← symbol截取' : '← 推算'}
                      </span>
                    : <AlertCircle className="h-3.5 w-3.5 text-danger/60 shrink-0" />}
                </div>
                {userFields.map((f) => {
                  const idx = fields.indexOf(f)
                  return (
                    <div key={idx} className="flex items-center gap-1.5 group">
                      <input
                        value={f.label}
                        onChange={(e) => updateField(idx, 'label', e.target.value)}
                        placeholder="显示名"
                        className="w-[72px] h-7 px-2 rounded-md border border-border bg-base text-[11px] text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/40"
                      />
                      <input
                        value={f.name}
                        onChange={(e) => updateField(idx, 'name', e.target.value)}
                        placeholder="字段名"
                        className="flex-1 h-7 px-2 rounded-md border border-border bg-base text-[11px] font-mono text-foreground placeholder:text-muted/40 focus:outline-none focus:border-accent/40"
                      />
                      <select
                        value={f.dtype}
                        onChange={(e) => updateField(idx, 'dtype', e.target.value)}
                        className="h-7 px-2 rounded-md border border-border bg-base text-[11px] text-foreground"
                      >
                        <option value="string">文本</option>
                        <option value="int">整数</option>
                        <option value="float">小数</option>
                        <option value="bool">布尔</option>
                      </select>
                      <button
                        onClick={() => removeField(idx)}
                        className="p-1 rounded text-muted/40 hover:text-danger hover:bg-danger/10 opacity-0 group-hover:opacity-100 transition-all"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {sourceMode === 'url' && (
            <div className="rounded-xl border border-border/60 bg-elevated/20 p-3 space-y-2">
              <div className="text-[11px] font-medium text-secondary">创建选项</div>
              <label className="flex items-center gap-2 text-[11px] text-secondary">
                <input
                  type="checkbox"
                  checked={savePull}
                  onChange={(e) => {
                    setSavePull(e.target.checked)
                    if (!e.target.checked) {
                      setImportNow(false)
                      setEnablePull(false)
                    }
                  }}
                  className="h-3.5 w-3.5 accent-accent"
                />
                保存为拉取配置
              </label>
              <label className="flex items-center gap-2 text-[11px] text-secondary">
                <input
                  type="checkbox"
                  checked={importNow}
                  onChange={(e) => {
                    setImportNow(e.target.checked)
                    if (e.target.checked) setSavePull(true)
                  }}
                  className="h-3.5 w-3.5 accent-accent"
                />
                创建后立即导入本次数据
              </label>
              <label className="flex items-center gap-2 text-[11px] text-secondary">
                <input
                  type="checkbox"
                  checked={enablePull}
                  onChange={(e) => {
                    setEnablePull(e.target.checked)
                    if (e.target.checked) setSavePull(true)
                  }}
                  className="h-3.5 w-3.5 accent-accent"
                />
                启用定时拉取
              </label>
            </div>
          )}

          {error && (
            <div className="text-[11px] text-danger bg-danger/5 rounded-lg px-3 py-2">{error}</div>
          )}
        </div>

        <div className="flex items-center justify-between px-6 py-4 border-t border-border/50 bg-elevated/20">
          <div className="text-[10px] text-muted/60">
            {sourceMode === 'url'
              ? '创建后可在扩展数据卡片中继续调整拉取配置'
              : sourceMode === 'file'
                ? '文件识别用于生成表结构，创建后仍可上传文件写入数据'
                : '手动创建后可通过文件、推送或拉取写入数据'}
          </div>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="px-4 py-2 rounded-lg bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors">
              取消
            </button>
            <button
              onClick={() => create.mutate()}
              disabled={!valid || create.isPending}
              className="px-5 py-2 rounded-lg bg-accent text-base text-xs font-medium hover:bg-accent/90 disabled:opacity-40 transition-colors shadow-sm shadow-accent/20"
            >
              {create.isPending ? '创建中…' : '创建'}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  )
}
