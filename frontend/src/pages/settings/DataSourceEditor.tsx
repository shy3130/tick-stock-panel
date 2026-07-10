import { useState, useEffect, useRef } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { KeyRound, Play, Plus, Save, Trash2, X, Zap, Check } from 'lucide-react'
import { api, type CustomSourceConfig, type DatasetConfig } from '@/lib/api'
import { toast } from '@/components/Toast'

// 暗色适配的标准输入框样式 (与 AI 页统一, bg-base 在暗色下为深色, 不会白底白字)
const INPUT_CLS =
  'w-full h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/40 text-xs text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/40 transition-shadow'

const DATASETS = ['daily', 'adj_factor', 'realtime', 'minute'] as const
type DatasetKey = typeof DATASETS[number]

const DATASET_LABEL: Record<DatasetKey, string> = {
  daily: '日K',
  adj_factor: '除权因子',
  realtime: '实时行情',
  minute: '分钟K',
}

const TARGET_FIELDS: Record<DatasetKey, string[]> = {
  daily: ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount'],
  adj_factor: ['symbol', 'trade_date', 'ex_factor'],
  realtime: ['symbol', 'name', 'last_price', 'prev_close', 'open', 'high', 'low', 'volume', 'amount', 'change_pct', 'change_amount', 'amplitude', 'turnover_rate', 'timestamp', 'session'],
  minute: ['symbol', 'datetime', 'open', 'high', 'low', 'close', 'volume', 'amount'],
}

// 内部字段的中文说明 (下拉选项展示用)
const FIELD_LABELS: Record<string, string> = {
  symbol: '股票代码 (如 000001.SZ / 600000.SH)',
  date: '交易日期 (YYYY-MM-DD)',
  datetime: '时间戳 (YYYY-MM-DD HH:MM:SS)',
  open: '开盘价',
  high: '最高价',
  low: '最低价',
  close: '收盘价',
  volume: '成交量 (手)',
  amount: '成交额 (元)',
  trade_date: '除权日期 (YYYY-MM-DD)',
  ex_factor: '复权因子',
  name: '股票名称',
  last_price: '最新价',
  prev_close: '昨收价',
  change_pct: '涨跌幅 (小数 0.0366=3.66%)',
  change_amount: '涨跌额',
  amplitude: '振幅 (小数)',
  turnover_rate: '换手率 (小数 0.05=5%)',
  timestamp: '时间戳',
  session: '交易时段',
}

function emptyConfig(): CustomSourceConfig {
  return { name: '', display_name: '', auth: { type: 'none' }, datasets: {} }
}

export function DataSourceEditor({
  existingName,
  initial,
  onCancel,
  onSaved,
  activeName,
  onActivate,
  onDelete,
}: {
  existingName?: string
  initial?: CustomSourceConfig | null
  onCancel: () => void
  onSaved: () => void
  activeName: string
  onActivate: (name: string) => void
  onDelete?: () => void
}) {
  const isNew = !existingName
  const [config, setConfig] = useState<CustomSourceConfig>(() => initial ? structuredClone(initial) : emptyConfig())
  const [activeTab, setActiveTab] = useState<DatasetKey>('daily')

  // 编辑现有源: 从后端拉完整配置 (每次挂载都重新拉, 不用缓存, 确保拿到最新保存的配置)
  const fetchCfg = useQuery({
    queryKey: ['data-source-detail', existingName],
    queryFn: () => api.dataSource(existingName!),
    enabled: !!existingName && !initial,
    staleTime: 0,
  })

  useEffect(() => {
    if (fetchCfg.data) {
      setConfig(structuredClone(fetchCfg.data))
    }
  }, [fetchCfg.data])

  const save = useMutation({
    mutationFn: () => {
      // 提交前校验: 每个已启用数据集必须填了 URL
      for (const [key, ds] of Object.entries(config.datasets)) {
        if (!ds.url.trim()) {
          throw new Error(`数据集「${DATASET_LABEL[key as DatasetKey] || key}」未填写接口 URL`)
        }
      }
      // 提交时去掉 field_map 里的 __pending_ 临时 key (未填外部字段名的草稿行)
      const cleaned: CustomSourceConfig = {
        ...config,
        name: config.name.toLowerCase().trim(),
        display_name: config.display_name.trim() || config.name.toLowerCase().trim(),
        datasets: Object.fromEntries(
          Object.entries(config.datasets).map(([k, ds]) => [
            k,
            {
              ...ds,
              field_map: Object.fromEntries(
                Object.entries(ds.field_map).filter(([src]) => !src.startsWith('__pending_'))
              ),
            },
          ])
        ),
      }
      return api.saveDataSource(cleaned)
    },
    onSuccess: () => {
      toast(isNew ? '数据源已创建' : '数据源已更新', 'success')
      // 保存后强制重新拉取最新配置, 让数据集开关状态正确刷新
      fetchCfg.refetch()
      onSaved()
    },
    onError: (e: Error) => {
      toast(e.message, 'error')
    },
  })

  const setDatasetEnabled = (key: DatasetKey, enabled: boolean) => {
    setConfig(prev => {
      const next = { ...prev, datasets: { ...prev.datasets } }
      if (enabled) {
        if (!next.datasets[key]) {
          next.datasets[key] = { url: '', method: 'POST', response_path: 'data', field_map: {} }
        }
      } else {
        delete next.datasets[key]
      }
      return next
    })
  }

  const updateDataset = (key: DatasetKey, patch: Partial<DatasetConfig>) => {
    setConfig(prev => ({
      ...prev,
      datasets: { ...prev.datasets, [key]: { ...prev.datasets[key], ...patch } as DatasetConfig },
    }))
  }

  const canSave = !!config.name.trim() && !save.isPending
  const loading = !!existingName && !initial && fetchCfg.isLoading
  const isActive = !isNew && activeName === existingName

  return (
    <section className="rounded-card border border-border bg-surface overflow-hidden">
      {/* 头部 */}
      <div className="px-6 py-4 border-b border-border/60 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className={`h-9 w-9 rounded-lg flex items-center justify-center ${isNew ? 'bg-accent/10' : 'bg-elevated'}`}>
            {isNew ? <Plus className="h-4 w-4 text-accent" /> : <KeyRound className="h-4 w-4 text-secondary" />}
          </div>
          <div>
            <h2 className="text-sm font-semibold text-foreground">{isNew ? '新增数据源' : '编辑数据源'}</h2>
            <p className="text-[11px] text-muted">{isNew ? '配置一个自定义 HTTP 数据源' : config.display_name || existingName}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {!isNew && isActive && (
            <span className="inline-flex items-center gap-1 text-[10px] text-accent bg-accent/10 px-2 py-1 rounded">
              <Check className="h-2.5 w-2.5" /> 使用中
            </span>
          )}
          {!isNew && !isActive && config.name.trim() && (
            <button
              onClick={() => onActivate(config.name.toLowerCase().trim())}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-btn bg-accent/10 text-accent text-xs font-medium hover:bg-accent/20 transition-colors"
            >
              <Zap className="h-3 w-3" /> 切换为当前
            </button>
          )}
          {!isNew && onDelete && (
            <button
              onClick={onDelete}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-btn text-xs text-muted hover:text-danger hover:bg-danger/10 transition-colors"
            >
              <Trash2 className="h-3 w-3" /> 删除
            </button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="p-12 text-center text-sm text-muted">加载配置...</div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr]">
          {/* 左: 基本信息 + 鉴权 + 数据集开关 */}
          <div className="p-5 space-y-4 border-r border-border/40">
            <Field label="名称" hint="小写字母/数字/下划线">
              <input
                value={config.name}
                onChange={e => setConfig({ ...config, name: e.target.value })}
                placeholder="my_tushare"
                disabled={!isNew}
                className={`${INPUT_CLS} w-full disabled:opacity-60`}
              />
            </Field>
            <Field label="显示名">
              <input
                value={config.display_name}
                onChange={e => setConfig({ ...config, display_name: e.target.value })}
                placeholder="我的 Tushare"
                className={`${INPUT_CLS} w-full`}
              />
            </Field>
            <Field label="鉴权">
              <div className="space-y-2">
                <select
                  value={config.auth.type}
                  onChange={e => setConfig({ ...config, auth: { ...config.auth, type: e.target.value } })}
                  className={`${INPUT_CLS} w-full`}
                >
                  <option value="none">无需鉴权</option>
                  <option value="bearer">Bearer Token</option>
                  <option value="header">自定义 Header</option>
                  <option value="query">Query 参数</option>
                </select>
                {config.auth.type !== 'none' && (
                  <input
                    value={config.auth.token_env ?? ''}
                    onChange={e => setConfig({ ...config, auth: { ...config.auth, token_env: e.target.value } })}
                    placeholder="环境变量名 (MY_TOKEN)"
                    className={`${INPUT_CLS} w-full`}
                  />
                )}
              </div>
            </Field>

            <div className="pt-2 border-t border-border/30 space-y-1.5">
              <div className="text-[10px] uppercase tracking-widest text-muted">数据集</div>
              {DATASETS.map(key => {
                const enabled = !!config.datasets[key]
                return (
                  <button
                    key={key}
                    onClick={() => setActiveTab(key)}
                    className={`w-full flex items-center gap-2 px-2.5 py-2 rounded-btn text-sm transition-colors ${
                      activeTab === key ? 'bg-elevated text-foreground' : 'text-secondary hover:bg-elevated/50'
                    }`}
                  >
                    <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${enabled ? 'bg-accent' : 'bg-muted/30'}`} />
                    <span className="flex-1 text-left">{DATASET_LABEL[key]}</span>
                    {enabled
                      ? <span className="text-[9px] text-accent">已配置</span>
                      : <span className="text-[9px] text-muted/50">回退 TF</span>
                    }
                    <Toggle
                      checked={enabled}
                      onChange={(e) => { e?.stopPropagation(); setDatasetEnabled(key, !enabled) }}
                    />
                  </button>
                )
              })}
            </div>
          </div>

          {/* 右: 当前数据集详情 */}
          <div className="p-5">
            <DatasetDetail
              key={activeTab}
              datasetKey={activeTab}
              cfg={config.datasets[activeTab]}
              providerName={config.name.toLowerCase().trim() || existingName || ''}
              onUpdate={(patch) => updateDataset(activeTab, patch)}
              onFieldMap={(fm) => updateDataset(activeTab, { field_map: fm })}
              onToggle={(v) => setDatasetEnabled(activeTab, v)}
            />
          </div>
        </div>
      )}

      {/* 底部保存栏 */}
      <div className="px-6 py-3.5 border-t border-border/60 flex items-center justify-between bg-elevated/20">
        <div className="text-[11px] text-muted">
          {Object.keys(config.datasets).length} 个数据集已配置
        </div>
        <div className="flex items-center gap-2">
          <button onClick={onCancel} className="px-3 py-1.5 rounded-btn text-sm text-secondary hover:text-foreground transition-colors">
            取消
          </button>
          <button
            onClick={() => save.mutate()}
            disabled={!canSave}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-accent text-white text-sm font-medium hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            <Save className="h-3.5 w-3.5" />
            {save.isPending ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </section>
  )
}

function DatasetDetail({
  datasetKey,
  cfg,
  providerName,
  onUpdate,
  onFieldMap,
  onToggle,
}: {
  datasetKey: DatasetKey
  cfg?: DatasetConfig
  providerName: string
  onUpdate: (patch: Partial<DatasetConfig>) => void
  onFieldMap: (fm: Record<string, string>) => void
  onToggle: (v: boolean) => void
}) {
  const enabled = !!cfg
  const [testSymbols, setTestSymbols] = useState('000001.SZ,600000.SH')
  const test = useMutation({
    mutationFn: () => api.testDataSource(
      providerName,
      datasetKey,
      testSymbols.split(/[,\s]+/).map(s => s.trim()).filter(Boolean),
    ),
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium text-foreground">{DATASET_LABEL[datasetKey]}</h3>
          <span className="text-[10px] text-muted/50 font-mono">{datasetKey}</span>
        </div>
        <Toggle checked={enabled} onChange={() => onToggle(!enabled)} />
      </div>

      <AnimatePresence mode="wait">
        {enabled && cfg ? (
          <motion.div
            key="content"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.12 }}
            className="space-y-4"
          >
            <div className="grid grid-cols-1 md:grid-cols-[1fr_90px] gap-2">
              <Field label="接口 URL">
                <input
                  value={cfg.url}
                  onChange={e => onUpdate({ url: e.target.value })}
                  placeholder="https://my.api/daily"
                  className={`${INPUT_CLS} w-full`}
                />
              </Field>
              <Field label="方法">
                <select
                  value={cfg.method}
                  onChange={e => onUpdate({ method: e.target.value })}
                  className={`${INPUT_CLS} w-full`}
                >
                  <option value="GET">GET</option>
                  <option value="POST">POST</option>
                </select>
              </Field>
            </div>

            <div className="grid grid-cols-3 gap-2">
              <Field label="批量">
                <input
                  value={cfg.batch ?? ''}
                  onChange={e => onUpdate({ batch: e.target.value ? Number(e.target.value) : null })}
                  placeholder="100"
                  className={`${INPUT_CLS} w-full`}
                />
              </Field>
              <Field label="RPM">
                <input
                  value={cfg.rpm ?? ''}
                  onChange={e => onUpdate({ rpm: e.target.value ? Number(e.target.value) : null })}
                  placeholder="200"
                  className={`${INPUT_CLS} w-full`}
                />
              </Field>
              <Field label="响应路径">
                <input
                  value={cfg.response_path}
                  onChange={e => onUpdate({ response_path: e.target.value })}
                  placeholder="data.list"
                  className={`${INPUT_CLS} w-full`}
                />
              </Field>
            </div>

            <div>
              <div className="flex items-center justify-between mb-2">
                <div className="text-[10px] uppercase tracking-widest text-muted">字段映射</div>
                <a
                  href="https://github.com/shy3130/tickflow-stock-panel/blob/main/docs/custom-data-source.md#用-ai-生成映射配置"
                  target="_blank"
                  rel="noreferrer"
                  className="text-[10px] text-accent/70 hover:text-accent hover:underline"
                >
                  AI 帮你整理映射 →
                </a>
              </div>
              <div className="text-[10px] text-muted/50 mb-1.5">
                外部字段 → 内部字段 · 不知道怎么填? 点上方链接用 AI 整理
              </div>
              <FieldMapEditor
                key={datasetKey}
                fieldMap={cfg.field_map}
                targets={TARGET_FIELDS[datasetKey]}
                onChange={onFieldMap}
              />
            </div>

            <div className="pt-3 border-t border-border/30">
              <div className="flex items-center gap-2 mb-2">
                <Play className="h-3 w-3 text-muted" />
                <span className="text-[11px] font-medium text-secondary">测试连接</span>
              </div>
              <div className="flex items-center gap-2">
                <input
                  value={testSymbols}
                  onChange={e => setTestSymbols(e.target.value)}
                  className={`${INPUT_CLS} flex-1 text-xs`}
                  placeholder="测试标的, 逗号分隔"
                />
                <button
                  onClick={() => test.mutate()}
                  disabled={test.isPending || !cfg.url || !providerName}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:text-foreground text-xs disabled:opacity-40 transition-colors"
                >
                  {test.isPending ? '测试中...' : '测试'}
                </button>
              </div>
              {test.data && (
                <div className="mt-2 rounded-lg border border-accent/20 bg-accent/5 px-3 py-2 text-xs">
                  <span className="text-accent font-medium">{test.data.rows}</span> 行
                  <span className="text-muted mx-1.5">·</span>
                  列: <span className="text-secondary">{test.data.columns.join(', ')}</span>
                </div>
              )}
              {test.isError && (
                <div className="mt-2 text-xs text-danger">测试失败, 请检查接口和映射</div>
              )}
            </div>
          </motion.div>
        ) : (
          <motion.div
            key="empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="py-12 text-center"
          >
            <div className="text-sm text-muted mb-1">{DATASET_LABEL[datasetKey]} 未启用</div>
            <div className="text-[11px] text-muted/60">启用后此数据集将由该自定义源提供, 未启用则回退 TickFlow</div>
            <button
              onClick={() => onToggle(true)}
              className="mt-3 inline-flex items-center gap-1 px-3 py-1.5 rounded-btn bg-accent/10 text-accent text-xs font-medium hover:bg-accent/20 transition-colors"
            >
              <Plus className="h-3 w-3" /> 启用{DATASET_LABEL[datasetKey]}
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function FieldMapEditor({
  fieldMap,
  targets,
  onChange,
}: {
  fieldMap: Record<string, string>
  targets: string[]
  onChange: (fm: Record<string, string>) => void
}) {
  // 内部用数组维护行的稳定身份, 避免 Record 在编辑空行时 key 漂移导致输入框失焦
  const [rows, setRows] = useState<Array<{ src: string; target: string; id: number }>>(() => {
    const entries = Object.entries(fieldMap)
    // 有意义的映射 (src 非空且非 pending)
    const real = entries.filter(([s, t]) => s.trim() && t.trim() && !s.startsWith('__pending_'))
    // pending 行 (外部字段名还没填, 但 target 已选)
    const pending = entries.filter(([s]) => s.startsWith('__pending_'))
    if (real.length > 0 || pending.length > 0) {
      return [
        ...real.map(([src, target], i) => ({ src, target, id: i + 1 })),
        ...pending.map(([, target], i) => ({ src: '', target, id: real.length + i + 1 })),
      ]
    }
    // fieldMap 为空时自动预填该数据集的所有内部字段 (外部字段名留空待填)
    return targets.map((target, i) => ({ src: '', target, id: i + 1 }))
  })
  const nextId = useRef(targets.length + 1)

  // rows 变化时立即同步到父级 (含空 src 的草稿行, 用 __pending_ 前缀保留)
  // 这样切换 tab 再切回来, 未填完的映射行不会丢
  useEffect(() => {
    const out: Record<string, string> = {}
    let pendingIdx = 0
    for (const r of rows) {
      const s = r.src.trim()
      if (s && r.target.trim()) {
        out[s] = r.target.trim()
      } else if (r.target.trim()) {
        // 外部字段名还没填, 用临时 key 保留 target 选择
        out[`__pending_${pendingIdx++}`] = r.target.trim()
      }
    }
    onChange(out)
  }, [rows])  // eslint-disable-line react-hooks/exhaustive-deps

  const emit = (newRows: typeof rows) => {
    setRows(newRows)
  }

  const updateRow = (id: number, patch: Partial<{ src: string; target: string }>) => {
    emit(rows.map(r => (r.id === id ? { ...r, ...patch } : r)))
  }

  const removeRow = (id: number) => {
    const filtered = rows.filter(r => r.id !== id)
    emit(filtered.length > 0 ? filtered : [{ src: '', target: '', id: nextId.current++ }])
  }

  const addRow = () => {
    emit([...rows, { src: '', target: '', id: nextId.current++ }])
  }

  const hasValid = rows.some(r => r.src.trim() && r.target.trim())

  return (
    <div className="space-y-1.5">
      {!hasValid && (
        <div className="text-[11px] text-muted/60 py-1">填写外部字段名后自动生效, 无需的字段可删除</div>
      )}
      {rows.map((row) => (
        <div key={row.id} className="grid grid-cols-[1fr_auto_1.2fr_auto] gap-1.5 items-center">
          <input
            value={row.src}
            onChange={e => updateRow(row.id, { src: e.target.value })}
            placeholder="外部字段名"
            className={`${INPUT_CLS} text-xs`}
          />
          <span className="text-muted/50 text-[10px]">→</span>
          <select
            value={targets.includes(row.target) ? row.target : ''}
            onChange={e => updateRow(row.id, { target: e.target.value })}
            className={`${INPUT_CLS} text-xs ${row.target && !targets.includes(row.target) ? 'text-warning' : ''}`}
          >
            <option value="">{row.target || '(选择)'}</option>
            {targets.map(t => (
              <option key={t} value={t}>
                {t}（{FIELD_LABELS[t] || t}）
              </option>
            ))}
          </select>
          <button
            onClick={() => removeRow(row.id)}
            className="text-muted hover:text-danger p-0.5 transition-colors"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
      <button
        onClick={addRow}
        className="inline-flex items-center gap-1 text-xs text-accent hover:text-accent/80 mt-1"
      >
        <Plus className="h-3 w-3" /> 添加映射
      </button>
    </div>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-widest text-muted">{label}</span>
        {hint && <span className="text-[9px] text-muted/50 normal-case">{hint}</span>}
      </div>
      {children}
    </div>
  )
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (e?: React.MouseEvent) => void }) {
  return (
    <button
      type="button"
      onClick={onChange}
      className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors ${checked ? 'bg-accent' : 'bg-elevated'}`}
      aria-pressed={checked}
    >
      <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform ${checked ? 'translate-x-[14px]' : 'translate-x-[2px]'}`} />
    </button>
  )
}
