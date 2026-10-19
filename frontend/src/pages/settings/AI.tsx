import { useState, useEffect, useRef, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Save, Loader2, Check, Wifi, Eye, EyeOff, Shield, SlidersHorizontal,
  Shuffle, ExternalLink, Trash2, FileCog, RefreshCw,
  Terminal, ChevronDown, Sparkles, Atom, Waves, Cloud, Gem, Moon,
} from 'lucide-react'
import { useSettings } from '@/lib/useSharedQueries'
import { api, type SettingsState, type SponsorModelItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { AnchorWrap } from '@/lib/useCardFlash'
import { copyText } from '@/lib/clipboard'
import { DEEPSEEK_FALLBACK_RANGE, MAX_DISCOUNT_FALLBACK_PCT, bandDiscountLabel, discountRangeLabel, loadSponsorModelItems, maxDiscountPct, useSponsorModelItems } from '@/lib/sponsor-discount'

// 折扣率 → 中文折数: 0.6→"6折", 0.35→"3.5折", 0.2→"2折"
const fmtDiscount = (rate: number) => {
  const fold = Math.round(rate * 100) / 10
  return `${Number.isInteger(fold) ? fold : fold.toFixed(1)}折`
}

// 上下文长度 → 紧凑显示: 1050000→"1M", 262144→"256K"
const fmtContext = (n?: number | null) => {
  if (!n || n <= 0) return ''
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`
  return String(n)
}

// 统一的输入框样式(与项目其他设置页一致)
const INPUT_CLS =
  'w-full h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/30 text-xs font-mono text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow'

// 空/非法输入 → undefined (后端保持原值), 合法正整数 → int
const toPositiveInt = (v: string) => {
  const n = parseInt(v, 10)
  return Number.isInteger(n) && n > 0 ? n : undefined
}

// 允许 0 (0=关闭): 轮次检查点等"0 有语义"的数值输入
const toIntAllowZero = (v: string) => {
  const n = parseInt(v, 10)
  return Number.isInteger(n) && n >= 0 ? n : undefined
}

const CODEX_PROVIDER = 'codex_cli'
const OPENAI_PROVIDER = 'openai'
const OPENAI_COMPAT_PROVIDER = 'openai_compat'
const CODEX_COMMAND = 'codex'
const DEFAULT_CODEX_MODEL = 'gpt-5.6-sol'
const DEFAULT_CODEX_REASONING_EFFORT = 'xhigh'
const DEFAULT_OPENAI_MODEL = 'gpt-5.5'
const DEFAULT_REASONING_EFFORT = 'high'
const SAVED_CODEX_OPTION_VALUE = '__saved_codex_config__'
const CODEX_REASONING_LABELS: Record<string, string> = {
  high: '高',
  xhigh: '极高',
}

type CodexModelOption = { label: string; value: string; model: string; effort: string; hint: string }

const CODEX_MODEL_OPTIONS: CodexModelOption[] = [
  { label: 'GPT-5.6 Sol · 极高（推荐）', value: 'gpt-5.6-sol:xhigh', model: 'gpt-5.6-sol', effort: 'xhigh', hint: '旗舰档，适合复杂金融分析与专业任务' },
  { label: 'GPT-5.6 Terra · 极高', value: 'gpt-5.6-terra:xhigh', model: 'gpt-5.6-terra', effort: 'xhigh', hint: '平衡智能、速度与使用成本' },
  { label: 'GPT-5.6 Luna · 极高', value: 'gpt-5.6-luna:xhigh', model: 'gpt-5.6-luna', effort: 'xhigh', hint: '适合成本敏感与高频分析任务' },
  { label: 'gpt-5.5 · 高', value: 'gpt-5.5:high', model: 'gpt-5.5', effort: 'high', hint: '使用 gpt-5.5 + high 推理档' },
  { label: 'gpt-5.5 · 极高', value: 'gpt-5.5:xhigh', model: 'gpt-5.5', effort: 'xhigh', hint: '使用 gpt-5.5 + xhigh 推理档' },
  { label: '跟随本机 Codex 默认', value: '', model: '', effort: '', hint: '使用本机 Codex CLI 配置的默认模型与推理强度' },
]

const codexModelLabel = (model?: string, effort?: string) => {
  if (!model && !effort) return '默认模型'
  const modelLabel = model || '默认模型'
  const effortLabel = effort ? CODEX_REASONING_LABELS[effort] ?? effort : ''
  return effortLabel ? `${modelLabel} · ${effortLabel}` : modelLabel
}

type AiPreset = {
  label: string
  provider?: string
  url: string
  model: string
  codexCommand?: string
  website: string
  websiteLabel: string
  description: ReactNode
  custom?: boolean
  sponsor?: boolean
  icon: React.ComponentType<{ className?: string }>
}

// 预设卡副行: 服务端点一眼可辨 (自定义/本机 CLI 无域名时给语义文案)
const presetMeta = (p: AiPreset) => {
  if (p.custom) return '手动配置'
  if (p.provider === CODEX_PROVIDER) return '本机 codex exec'
  try {
    return new URL(p.url).hostname
  } catch {
    return p.url
  }
}

/** RunningHub 预设描述: 折扣区间取自实时模型定价; 取不到时回退到集中的兜底文案。 */
function SponsorIntro() {
  const items = useSponsorModelItems()
  const range = (items && discountRangeLabel(items)) ?? DEEPSEEK_FALLBACK_RANGE
  const maxPct = (items && maxDiscountPct(items)) ?? MAX_DISCOUNT_FALLBACK_PCT
  return (
    <>
      本项目赞助商 · OpenAI 兼容中转，单一接口直连 400+ 主流大模型，<span className="font-medium text-amber-600 dark:text-amber-400">Claude、ChatGPT、Gemini</span> 等国际模型直连稳定不掉线。
      <span className="font-medium text-amber-600 dark:text-amber-400">DeepSeek v4.1 Flash {range}</span>，
      <span className="text-muted">全场最高优惠 {maxPct}%</span>，
      <span className="font-medium text-amber-600 dark:text-amber-400">邀请链接注册赠送 1000 RH 积分</span><span className="text-muted">(积分可用于视频生成)</span>。
    </>
  )
}

const PRESETS: AiPreset[] = [
  { label: '自定义', url: '', model: '', website: '', websiteLabel: '', description: '不自动填充任何配置，完全手动填写 API 地址、模型和密钥。', custom: true, icon: SlidersHorizontal },
  { label: 'RunningHub', url: 'https://llm.runninghub.ai/v1', model: 'openai/gpt-6-astra-saver', website: 'https://www.runninghub.ai/zh-cn/call-api/llm/models?source=github&inviteCode=edt5wh7c', websiteLabel: 'www.runninghub.ai · 赞助', sponsor: true, icon: Sparkles, description: <SponsorIntro /> },
  { label: 'OpenAI', provider: OPENAI_PROVIDER, url: 'https://api.openai.com/v1', model: DEFAULT_OPENAI_MODEL, website: 'https://platform.openai.com/', websiteLabel: 'platform.openai.com', description: 'OpenAI 官方接口，可单独配置模型支持的推理强度。', icon: Atom },
  { label: 'DeepSeek', url: 'https://api.deepseek.com', model: 'deepseek-v4-pro', website: 'https://www.deepseek.com/', websiteLabel: 'deepseek.com', description: 'DeepSeek 官方 OpenAI 兼容接口。', icon: Waves },
  { label: '通义千问', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-3.6plus', website: 'https://tongyi.aliyun.com/', websiteLabel: 'tongyi.aliyun.com', description: '阿里云 DashScope 兼容模式接口。', icon: Cloud },
  { label: '智谱 GLM', url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2', website: 'https://open.bigmodel.cn/', websiteLabel: 'open.bigmodel.cn', description: '智谱 AI 官方 OpenAI 兼容接口。', icon: Gem },
  { label: 'Kimi', url: 'https://api.moonshot.cn/v1', model: 'kimi-k2.7-code', website: 'https://platform.moonshot.cn/', websiteLabel: 'platform.moonshot.cn', description: '月之暗面 Moonshot 官方 OpenAI 兼容接口，支持超长上下文。', icon: Moon },
  { label: 'Codex CLI', provider: CODEX_PROVIDER, url: '', model: DEFAULT_CODEX_MODEL, codexCommand: CODEX_COMMAND, website: 'https://developers.openai.com/codex/noninteractive', websiteLabel: 'codex exec', description: '调用本机 Codex CLI 的 codex exec, 适合已登录 ChatGPT/Codex 的本地环境。', icon: Terminal },
]

const findPreset = (provider: string, baseUrl: string, codexCommand: string) => PRESETS.find(p => {
  if (p.custom || (p.provider ?? OPENAI_COMPAT_PROVIDER) !== provider) return false
  if (provider === OPENAI_PROVIDER) return true
  return provider === CODEX_PROVIDER ? p.codexCommand === codexCommand : p.url === baseUrl
}) ?? PRESETS[0]

export function SettingsAIPanel({ highlight }: { highlight?: string } = {}) {
  const qc = useQueryClient()
  const settings = useSettings()
  const s = settings.data

  const [provider, setProvider] = useState(OPENAI_COMPAT_PROVIDER)
  const [protocol, setProtocol] = useState('chat_completions')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [reasoningEffort, setReasoningEffort] = useState(DEFAULT_REASONING_EFFORT)
  const [codexModel, setCodexModel] = useState('')
  const [codexReasoningEffort, setCodexReasoningEffort] = useState('')
  const [codexCommand, setCodexCommand] = useState(CODEX_COMMAND)
  const [customUa, setCustomUa] = useState(false)
  const [userAgent, setUserAgent] = useState('')
  const [maxOutputTokens, setMaxOutputTokens] = useState('')
  const [contextWindow, setContextWindow] = useState('')
  const [roundCheckpoint, setRoundCheckpoint] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [pathCopied, setPathCopied] = useState(false)
  const [reloaded, setReloaded] = useState(false)
  // 赞助商预设的模型列表下拉（/models 接口 + 关键词过滤）
  const [modelsOpen, setModelsOpen] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsError, setModelsError] = useState('')
  const [modelItems, setModelItems] = useState<SponsorModelItem[]>([])
  const [modelFilter, setModelFilter] = useState('')
  const modelBoxRef = useRef<HTMLDivElement | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [selectedPresetLabel, setSelectedPresetLabel] = useState(PRESETS[0].label)
  const directDrafts = useRef({
    custom: { baseUrl: '', model: '' },
    openai: { baseUrl: 'https://api.openai.com/v1', model: DEFAULT_OPENAI_MODEL },
  })
  const draftsInitialized = useRef(false)
  // 用户一旦手动交互过(选预设/改字段), settings 快照到达或刷新时不再整段重置表单 —
  // 否则晚到的 s 会把选中的赞助商预设冲回保存态, 正在显示的模型下拉被直接卸载
  // (表现为「刷新后要点两次模型按钮才出列表」)。
  const userEditedRef = useRef(false)

  const isCodexProvider = provider === CODEX_PROVIDER
  const isOpenAIProvider = provider === OPENAI_PROVIDER
  const savedCodexProvider = s?.ai_provider === CODEX_PROVIDER
  const configured = s?.ai_configured ?? (savedCodexProvider ? !!(s?.ai_codex_command ?? CODEX_COMMAND) : s?.has_ai_key)
  const selectedPreset = PRESETS.find(p => p.label === selectedPresetLabel) ?? PRESETS[0]
  const savedCodexModel = s?.ai_codex_model ?? (savedCodexProvider ? (s?.ai_model ?? '') : '')
  const savedCodexEffort = s?.ai_codex_reasoning_effort ?? ''
  const savedCodexOptionKnown = CODEX_MODEL_OPTIONS.some(option =>
    option.model === savedCodexModel && option.effort === savedCodexEffort,
  )
  const savedCodexOption: CodexModelOption | null =
    (savedCodexModel || savedCodexEffort) && !savedCodexOptionKnown
      ? {
          label: `${codexModelLabel(savedCodexModel, savedCodexEffort)}（当前配置）`,
          value: SAVED_CODEX_OPTION_VALUE,
          model: savedCodexModel,
          effort: savedCodexEffort,
          hint: '保留项目中已保存的模型与推理档；此兼容项不可编辑',
        }
      : null
  const codexModelOptions = savedCodexOption
    ? [savedCodexOption, ...CODEX_MODEL_OPTIONS]
    : CODEX_MODEL_OPTIONS
  const selectedCodexModelOption = codexModelOptions.find(option =>
    option.model === codexModel && option.effort === codexReasoningEffort,
  ) ?? CODEX_MODEL_OPTIONS[0]
  const codexModelSelectValue = selectedCodexModelOption.value
  const canSave = isCodexProvider ? true : !!baseUrl.trim() && !!model.trim()
  // 测试按钮可用性: codex 恒可(探测 CLI 可用性); 其余需要地址+模型, 且
  // Key 草稿或已保存至少其一 (留空回退已保存 Key, 改模型/协议不必重输)
  const canTest = isCodexProvider || (!!baseUrl.trim() && !!model.trim() && (!!apiKey.trim() || !!configured))

  // 表单 ← settings 快照的单向同步 (初次加载与「从文件重新加载」共用)
  const syncFormFromSettings = (st: SettingsState) => {
    // 未配置过 AI (无 api_key): 字段留空, 默认选中"自定义"预设, 不预填充后端默认值
    const unconfigured = !st.has_ai_key && !st.ai_configured
    const savedProvider = st.ai_provider ?? OPENAI_COMPAT_PROVIDER
    const savedBaseUrl = unconfigured ? '' : (st.ai_base_url ?? '')
    const savedOpenAIModel = unconfigured ? '' : (st.ai_openai_model ?? (savedProvider !== CODEX_PROVIDER ? st.ai_model : '') ?? '')
    const savedPreset = unconfigured ? PRESETS[0] : findPreset(savedProvider, savedBaseUrl, st.ai_codex_command ?? CODEX_COMMAND)
    if (!draftsInitialized.current) {
      const officialOpenAI = PRESETS.find(p => p.provider === OPENAI_PROVIDER)
      if (savedProvider === OPENAI_PROVIDER || (savedProvider === CODEX_PROVIDER && savedBaseUrl === officialOpenAI?.url)) {
        directDrafts.current.openai = { baseUrl: savedBaseUrl, model: savedOpenAIModel }
      } else if (findPreset(OPENAI_COMPAT_PROVIDER, savedBaseUrl, CODEX_COMMAND).custom) {
        directDrafts.current.custom = { baseUrl: savedBaseUrl, model: savedOpenAIModel }
      }
      draftsInitialized.current = true
    }
    setProvider(savedProvider)
    setSelectedPresetLabel(savedPreset.label)
    setProtocol(savedProvider === CODEX_PROVIDER ? 'chat_completions' : (st.ai_protocol ?? 'chat_completions'))
    setBaseUrl(savedBaseUrl)
    setModel(savedOpenAIModel)
    setReasoningEffort(st.ai_reasoning_effort ?? DEFAULT_REASONING_EFFORT)
    setCodexModel(st.ai_codex_model ?? (savedProvider === CODEX_PROVIDER ? st.ai_model : '') ?? '')
    setCodexReasoningEffort(st.ai_codex_reasoning_effort ?? '')
    setCodexCommand(st.ai_codex_command ?? CODEX_COMMAND)
    const ua = st.ai_user_agent ?? ''
    setCustomUa(!!ua)
    setUserAgent(ua)
    setMaxOutputTokens(String(st.ai_max_output_tokens ?? 16384))
    setContextWindow(String(st.ai_context_window ?? 128000))
    setRoundCheckpoint(String(st.ai_round_checkpoint ?? 100))
    setApiKey('')
  }

  useEffect(() => {
    if (!s) return
    // 用户已交互: 表单是用户草稿, 晚到/重复的 settings 快照不得覆盖
    // (保存成功后的字段同步由 save onSuccess 负责, 清空由 clear 负责)。
    if (userEditedRef.current) return
    syncFormFromSettings(s)
  }, [s])

  // 「从文件重新加载」: 用户外部编辑 secrets.json 后, 显式重读磁盘并覆盖表单。
  // 后端按 mtime 签名缓存失效, refetch 即取到最新文件内容, 无需重启服务。
  const reloadFromFile = async () => {
    userEditedRef.current = false
    draftsInitialized.current = false
    setTestResult(null)
    try {
      const fresh = await settings.refetch()
      if (fresh.data) syncFormFromSettings(fresh.data)
      setReloaded(true)
      window.setTimeout(() => setReloaded(false), 2000)
    } finally {
      draftsInitialized.current = true
    }
  }

  const copySecretsPath = async () => {
    if (!s?.secrets_path) return
    if (await copyText(s.secrets_path)) {
      setPathCopied(true)
      window.setTimeout(() => setPathCopied(false), 2000)
    }
  }

  const payload = () => ({
    provider,
    base_url: baseUrl,
    api_key: apiKey || undefined,
    model: isCodexProvider ? codexModel : model,
    ...( !isCodexProvider ? { protocol } : {}),
    ...(isOpenAIProvider ? { reasoning_effort: reasoningEffort } : {}),
    codex_command: isCodexProvider ? CODEX_COMMAND : codexCommand,
    codex_reasoning_effort: isCodexProvider ? codexReasoningEffort : '',
    user_agent: customUa ? userAgent : '',
    max_output_tokens: toPositiveInt(maxOutputTokens),
    context_window: toPositiveInt(contextWindow),
    round_checkpoint: toIntAllowZero(roundCheckpoint),
  })

  const save = useMutation({
    mutationFn: () => api.saveAiSettings(payload()),
    onSuccess: (result) => {
      setSaved(true)
      setApiKey('')
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: result.ai_provider ?? provider,
        ai_protocol: result.ai_protocol ?? (!isCodexProvider ? protocol : 'chat_completions'),
        ai_base_url: baseUrl,
        ai_model: result.ai_model ?? (isCodexProvider ? codexModel : model),
        ai_openai_model: result.ai_openai_model ?? model,
        ai_reasoning_effort: result.ai_reasoning_effort ?? reasoningEffort,
        ai_codex_model: result.ai_codex_model ?? codexModel,
        ai_codex_command: result.ai_codex_command ?? (isCodexProvider ? CODEX_COMMAND : codexCommand),
        ai_codex_reasoning_effort: result.ai_codex_reasoning_effort ?? (isCodexProvider ? codexReasoningEffort : ''),
        ai_configured: result.ai_configured ?? (isCodexProvider ? true : (apiKey ? true : prev.ai_configured)),
        ai_max_output_tokens: result.ai_max_output_tokens ?? toPositiveInt(maxOutputTokens),
        ai_context_window: result.ai_context_window ?? toPositiveInt(contextWindow),
        ai_round_checkpoint: result.ai_round_checkpoint ?? toIntAllowZero(roundCheckpoint),
        ...(apiKey ? {
          has_ai_key: true,
          ai_api_key_masked: `${apiKey.slice(0, 4)}......${apiKey.slice(-4)}`,
        } : {}),
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
      setTimeout(() => setSaved(false), 2000)
    },
  })

  const clear = useMutation({
    mutationFn: () => api.clearAiSettings(),
    onSuccess: () => {
      setConfirmClear(false)
      userEditedRef.current = false
      setProvider(OPENAI_COMPAT_PROVIDER)
      setSelectedPresetLabel(PRESETS[0].label)
      setProtocol('chat_completions')
      setBaseUrl('')
      setApiKey('')
      setModel('')
      setReasoningEffort(DEFAULT_REASONING_EFFORT)
      setCodexModel('')
      setCodexReasoningEffort('')
      setCodexCommand(CODEX_COMMAND)
      directDrafts.current = {
        custom: { baseUrl: '', model: '' },
        openai: { baseUrl: 'https://api.openai.com/v1', model: DEFAULT_OPENAI_MODEL },
      }
      setMaxOutputTokens('16384')
      setContextWindow('128000')
      setRoundCheckpoint('100')
      setTestResult(null)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: OPENAI_COMPAT_PROVIDER,
        ai_protocol: 'chat_completions',
        ai_base_url: '',
        ai_model: '',
        ai_openai_model: '',
        ai_reasoning_effort: DEFAULT_REASONING_EFFORT,
        ai_codex_model: '',
        ai_codex_command: CODEX_COMMAND,
        ai_codex_reasoning_effort: '',
        ai_max_output_tokens: 16384,
        ai_context_window: 128000,
        has_ai_key: false,
        ai_configured: false,
        ai_api_key_masked: '',
      } : prev)
      qc.invalidateQueries({ queryKey: QK.settings })
    },
  })

  const genRandomUa = () => {
    const major = 128 + Math.floor(Math.random() * 8)
    const platforms = [
      'Windows NT 10.0; Win64; x64',
      'Macintosh; Intel Mac OS X 10_15_7',
      'X11; Linux x86_64',
    ]
    const pf = platforms[Math.floor(Math.random() * platforms.length)]
    setUserAgent(`Mozilla/5.0 (${pf}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`)
  }

  const handlePreset = (p: AiPreset) => {
    userEditedRef.current = true
    setSelectedPresetLabel(p.label)
    if (p.custom) {
      setProvider(OPENAI_COMPAT_PROVIDER)
      setBaseUrl(directDrafts.current.custom.baseUrl)
      setModel(directDrafts.current.custom.model)
      return
    }
    if (p.provider === CODEX_PROVIDER) {
      setProvider(CODEX_PROVIDER)
      setCodexModel(p.model)
      setCodexReasoningEffort(DEFAULT_CODEX_REASONING_EFFORT)
      setCodexCommand(CODEX_COMMAND)
      return
    }
    const nextProvider = p.provider ?? OPENAI_COMPAT_PROVIDER
    setProvider(nextProvider)
    if (nextProvider === OPENAI_PROVIDER) {
      setBaseUrl(directDrafts.current.openai.baseUrl)
      setModel(directDrafts.current.openai.model)
    } else {
      setBaseUrl(p.url)
      setModel(p.model)
    }
  }

  const handleBaseUrlChange = (value: string) => {
    userEditedRef.current = true
    setBaseUrl(value)
    if (selectedPreset.custom) directDrafts.current.custom.baseUrl = value
    if (isOpenAIProvider) directDrafts.current.openai.baseUrl = value
  }

  const handleModelChange = (value: string) => {
    userEditedRef.current = true
    setModel(value)
    if (selectedPreset.custom) directDrafts.current.custom.model = value
    if (isOpenAIProvider) directDrafts.current.openai.model = value
  }

  // 赞助商预设: 经后端代理拉取 /models 全量模型列表供下拉选择
  // (浏览器直连会被 RunningHub 网关按 Origin 过滤, 只返回国产模型)
  const fetchModelOptions = async () => {
    setModelsError('')
    setModelFilter('')
    setModelsOpen(true)
    setModelsLoading(true)
    try {
      // 共享加载器自带 60s TTL 与并发合并, 与赞助商介绍条共用同一份数据
      const items = await loadSponsorModelItems()
      setModelItems(items)
    } catch (error) {
      setModelItems([])
      setModelsError(error instanceof Error ? error.message : '获取模型列表失败')
    } finally {
      setModelsLoading(false)
    }
  }

  const filteredModelItems = modelFilter.trim()
    ? modelItems.filter(m => m.id.toLowerCase().includes(modelFilter.trim().toLowerCase()))
    : modelItems
  // 有折扣的排前面(同组按名称), 优惠一目了然; 无定价数据时退化为纯 id 列表
  const sortedModelItems = [...filteredModelItems].sort((a, b) => {
    const da = a.discount_rate != null && a.discount_rate < 1 ? 0 : 1
    const db = b.discount_rate != null && b.discount_rate < 1 ? 0 : 1
    return da - db || a.id.localeCompare(b.id)
  })

  // 下拉打开时点击外部关闭
  useEffect(() => {
    if (!modelsOpen) return
    const onDocMouseDown = (e: MouseEvent) => {
      if (modelBoxRef.current && !modelBoxRef.current.contains(e.target as Node)) setModelsOpen(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [modelsOpen])

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      // 只探测当前表单值, 绝不保存 — 测试失败不能把坏配置写进已保存配置。
      const r = await api.testAiSettings({
        provider,
        ...(isCodexProvider ? {} : { base_url: baseUrl.trim(), protocol }),
        model: (isCodexProvider ? codexModel : model).trim(),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
      })
      setTestResult({
        ok: r.ok,
        msg: r.ok ? `连通成功 · ${r.model ?? provider}` : (r.error ?? '未知错误'),
      })
    } catch (e: any) {
      setTestResult({ ok: false, msg: String(e?.message ?? '测试失败') })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="max-w-3xl">
      <AnchorWrap highlight={highlight} anchor="ai-connection">
      {/* 拆成多张卡 (与 数据源 页同构): 原先 7 个区块挤在一张卡里, 只靠细分割线
          区分, 视觉上是一堵「行墙」, 且各区块留白不一致。 */}
      <div className="space-y-4">
      {/* 面板页眉 (非卡片): 标题 / 当前状态 / 连接测试。
          这三者是在描述「当前配置处于什么状态」, 不是可编辑的配置项 ——
          套卡片会得到一个只有标题和一行字的空框, 且把本该属于「接口配置」
          的请求方式开关顶到了页面最上方。 */}
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <Sparkles className="h-4 w-4 shrink-0 text-secondary" />
            <h2 className="text-sm font-medium text-foreground">AI 服务</h2>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${configured ? 'bg-bear' : 'bg-warning'}`} />
            <span className="min-w-0 truncate text-xs text-secondary">
              <span className="font-medium text-foreground">{configured ? '已配置' : '未配置'}</span>
              {' · '}
              {configured ? (
                savedCodexProvider
                  ? <><span className="font-mono">{s?.ai_codex_command ?? CODEX_COMMAND}</span> · <span className="font-mono">{codexModelLabel(s?.ai_model, s?.ai_codex_reasoning_effort)}</span></>
                  : <><span className="font-mono">{s?.ai_model}</span><span className="text-muted"> · {s?.ai_api_key_masked}</span></>
              ) : (
                <span className="text-muted">{isCodexProvider ? '使用本机 codex exec, 无需填写 Key' : '填写下方参数并保存即可启用'}</span>
              )}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2 pt-0.5">
          {testResult && (
            <span title="测试只验证当前表单, 不会保存配置; 通过后请点「保存配置」生效"
              className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] ${testResult.ok ? 'border-bear/25 bg-bear/[0.06] text-bear' : 'border-danger/25 bg-danger/[0.06] text-danger'}`}>
              <span className={`h-1 w-1 rounded-full ${testResult.ok ? 'bg-bear' : 'bg-danger'}`} />
              {testResult.msg}
            </span>
          )}
          {(configured || canTest) && (
            <button onClick={handleTest} disabled={testing}
              title="测试当前表单的连接, 不保存配置"
              className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface active:scale-[0.98] disabled:opacity-50">
              {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
              {testing ? '测试中' : '测试'}
            </button>
          )}
        </div>
      </div>

      <section className="overflow-hidden rounded-card border border-border bg-surface">
        {/* 服务商 */}
        <div className="px-5 py-4">
          <SectionHeader label="服务商" />
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {PRESETS.map(p => {
              const active = selectedPreset?.label === p.label
              const Icon = p.icon
              return (
                <button key={p.label} type="button" onClick={() => handlePreset(p)} aria-pressed={active}
                  className={`group relative flex flex-col gap-1.5 rounded-lg border p-2.5 text-left transition-all duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface active:scale-[0.98] ${
                    active
                      ? 'border-accent/60 bg-accent/[0.09] shadow-[0_0_0_1px_rgba(59,130,246,0.12),0_0_12px_-3px_rgba(59,130,246,0.45)]'
                      : 'border-border/60 bg-base/40 hover:border-accent/30 hover:bg-base'
                  }`}>
                  {/* 选中标记: 8 张卡同构时仅靠描边难以辨认, 左上角给一道主色竖标 */}
                  <span aria-hidden="true"
                    className={`absolute left-0 top-1/2 h-5 w-[2px] -translate-y-1/2 rounded-r-full bg-accent transition-opacity duration-150 ${
                      active ? 'opacity-100' : 'opacity-0'
                    }`} />
                  <span className="flex items-center gap-2">
                    <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition-colors ${
                      active ? 'bg-accent/20 text-accent' : 'bg-elevated/60 text-muted group-hover:text-secondary'
                    }`}>
                      <Icon className="h-3.5 w-3.5" />
                    </span>
                    <span className={`truncate text-xs font-medium ${active ? 'text-foreground' : 'text-secondary'}`}>{p.label}</span>
                    {p.sponsor && <span className="ml-auto shrink-0 rounded-full bg-amber-400/15 px-1.5 py-px text-[9px] font-semibold leading-none text-amber-600 dark:text-amber-400">优惠</span>}
                  </span>
                  <span className="truncate font-mono text-[10px] text-muted">{presetMeta(p)}</span>
                </button>
              )
            })}
          </div>
          {selectedPreset && (
            <div className="mt-2.5 rounded-lg bg-base/40 px-3.5 py-2.5">
              <p className="text-[11px] leading-relaxed text-secondary">{selectedPreset.description}</p>
              {selectedPreset.website && (
                <a href={selectedPreset.website} target="_blank" rel="noreferrer"
                  className={`mt-1.5 inline-flex items-center gap-1 text-[11px] transition-colors ${selectedPreset.sponsor ? 'font-medium text-orange-500 hover:text-orange-600 dark:text-orange-400 dark:hover:text-orange-300' : 'text-muted hover:text-accent'}`}>
                  {selectedPreset.websiteLabel}
                  <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>
          )}
        </div>
      </section>

      <section className="overflow-hidden rounded-card border border-border bg-surface">
        {/* 接口配置 */}
        <div className="px-5 py-4">
          {/* 标题行: 左侧区段名, 右侧请求方式 (属于本区段内可编辑项, 与下方表单同组) */}
          <div className="mb-3 flex items-center gap-3">
            <SectionHeader label={isCodexProvider ? '本地 CLI 配置' : '接口配置'} inline />
            {!isCodexProvider && (
              <div className="ml-auto flex items-center rounded-full border border-border/40 bg-base/50 p-0.5"
                title="Chat Completions 兼容面最广; Responses 为 OpenAI 官方新协议, 仅官方及少数网关支持。不确定就保持默认。">
                {(['chat_completions', 'responses'] as const).map(p => (
                  <button key={p} type="button" onClick={() => setProtocol(p)} aria-pressed={protocol === p}
                    className={`rounded-full px-2.5 py-1 font-mono text-[10px] leading-none transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 ${
                      protocol === p ? 'bg-accent/15 text-accent' : 'text-muted hover:text-secondary'
                    }`}>
                    {p === 'chat_completions' ? 'Chat Completions' : 'Responses'}
                  </button>
                ))}
              </div>
            )}
            {isCodexProvider && (
              <span className="ml-auto inline-flex items-center gap-1.5 text-[10px] text-muted" title="Use local Codex CLI via codex exec">
                <span className="rounded-full border border-border/40 bg-base/50 px-1.5 py-px font-mono">codex exec</span>
                CLI
              </span>
            )}
          </div>
          <div className="space-y-4">
          {isCodexProvider ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field label="CLI 命令" hint="固定使用默认 codex 命令, 由后端自动解析本机 Codex Desktop/CLI, 不支持自定义可执行路径。">
                <div className={`${INPUT_CLS} flex items-center text-muted/80 select-none`} aria-label="Codex CLI command">
                  {CODEX_COMMAND}
                </div>
              </Field>
              <Field
                label="模型 / 推理档"
                hint={selectedCodexModelOption.hint}
              >
                <select
                  value={codexModelSelectValue}
                  onChange={e => {
                    const value = e.target.value
                    const option = codexModelOptions.find(item => item.value === value) ?? CODEX_MODEL_OPTIONS[0]
                    setCodexModel(option.model)
                    setCodexReasoningEffort(option.effort)
                  }}
                  className={INPUT_CLS}
                >
                  {codexModelOptions.map(option => (
                    <option key={option.value || 'codex-local-default'} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </Field>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <Field label="API 地址">
                  <input type="text" value={baseUrl} onChange={e => handleBaseUrlChange(e.target.value)} placeholder="https://llm.runninghub.ai/v1" className={INPUT_CLS} />
                </Field>
                <Field label="模型">
                  <div ref={modelBoxRef} className="relative">
                    <input type="text" value={model} onChange={e => handleModelChange(e.target.value)} placeholder="gpt-5.6-sol" className={`${INPUT_CLS} ${selectedPreset?.sponsor ? 'pr-9' : ''}`} />
                    {selectedPreset?.sponsor && (
                      <button type="button" onClick={fetchModelOptions} aria-label="获取模型列表"
                        className="absolute right-1.5 top-1/2 flex h-6 w-7 -translate-y-1/2 items-center justify-center rounded-md border border-border/40 bg-base text-muted transition-colors hover:border-accent/40 hover:text-accent">
                        {modelsLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronDown className="h-3.5 w-3.5" />}
                      </button>
                    )}
                    {selectedPreset?.sponsor && modelsOpen && (
                      <div className="absolute z-20 mt-1 w-full rounded-lg border border-border/40 bg-base shadow-lg">
                        <div className="border-b border-border/30 p-2">
                          <input type="text" value={modelFilter} onChange={e => setModelFilter(e.target.value)}
                            placeholder="搜索模型..." autoFocus
                            className="h-7 w-full rounded-md bg-base px-2 text-xs ring-1 ring-border/30 focus:outline-none focus:ring-accent/40" />
                        </div>
                        <div className="max-h-56 overflow-y-auto py-1">
                          {modelsLoading ? (
                            <div className="flex items-center justify-center gap-1.5 py-6 text-xs text-muted">
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />加载中...
                            </div>
                          ) : modelsError ? (
                            <div className="px-3 py-4 text-center text-xs text-muted">获取失败: {modelsError}</div>
                          ) : sortedModelItems.length === 0 ? (
                            <div className="px-3 py-4 text-center text-xs text-muted">{modelItems.length ? '无匹配模型' : '未获取到模型'}</div>
                          ) : sortedModelItems.map(m => {
                            const discounted = m.discount_rate != null && m.discount_rate < 1
                            const bands = bandDiscountLabel(m)
                            const priceHint = [
                              bands ?? undefined,
                              m.offpeak_label ? `闲时时段: ${m.offpeak_label}` : undefined,
                              [m.input_price, m.output_price].some(v => v != null)
                                ? `输入 $${m.input_price ?? '-'}/1K tokens · 输出 $${m.output_price ?? '-'}/1K tokens`
                                : undefined,
                            ].filter(Boolean).join('\n') || undefined
                            const isOffPeak = m.current_band === 'OFF_PEAK'
                            return (
                              <button key={m.id} type="button" onClick={() => { handleModelChange(m.id); setModelsOpen(false) }} title={priceHint}
                                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors hover:bg-accent/10 ${model === m.id ? 'text-accent' : 'text-secondary'}`}>
                                <span className="truncate font-mono text-xs">{m.id}</span>
                                <span className="ml-auto flex shrink-0 items-center gap-1.5">
                                  {fmtContext(m.context_length) && (
                                    <span className="text-[10px] text-muted">{fmtContext(m.context_length)}</span>
                                  )}
                                  {bands ? (
                                    <span
                                      className={`rounded-full px-1.5 py-px text-[10px] font-medium ${
                                        isOffPeak
                                          ? 'bg-emerald-400/15 text-emerald-600 dark:text-emerald-400'
                                          : 'bg-amber-400/15 text-amber-600 dark:text-amber-400'
                                      }`}
                                      title={m.offpeak_label ? `闲时时段: ${m.offpeak_label}` : undefined}
                                    >
                                      {bands}{isOffPeak ? ' · 当前闲时' : ' · 当前忙时'}
                                    </span>
                                  ) : discounted && (
                                    <span className="rounded-full bg-amber-400/15 px-1.5 py-px text-[10px] font-medium text-amber-600 dark:text-amber-400">
                                      {fmtDiscount(m.discount_rate!)}
                                    </span>
                                  )}
                                </span>
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                </Field>
              </div>

              {isOpenAIProvider && (
                <div className="max-w-[220px]">
                  <Field label="推理强度" hint="模型支持的推理档位 (如 high / xhigh); 非推理模型留空即可。">
                    <input type="text" value={reasoningEffort} onChange={e => setReasoningEffort(e.target.value)} placeholder={DEFAULT_REASONING_EFFORT} className={INPUT_CLS} />
                  </Field>
                </div>
              )}

              <Field label="API Key">
                <div className="relative">
                  <input type={showKey ? 'text' : 'password'} value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={configured ? `${s?.ai_api_key_masked} · 留空不修改` : 'sk-...'} className={`${INPUT_CLS} pr-9`} />
                  <button onClick={() => setShowKey(v => !v)} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted/40 hover:text-muted" tabIndex={-1} aria-label={showKey ? '隐藏' : '显示'}>
                    {showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </Field>

              <div className="flex items-center gap-2 border-t border-border/30 pt-3.5">
                <span className="text-xs font-medium text-secondary">自定义 User-Agent</span>
                <span className="text-[10px] text-muted">绕过 CDN/WAF 的 Bot 拦截</span>
                <div className="ml-auto"><Toggle checked={customUa} onChange={() => setCustomUa(v => !v)} /></div>
              </div>
              {customUa && (
                <div className="flex gap-2">
                  <input type="text" value={userAgent} onChange={e => setUserAgent(e.target.value)} placeholder="粘贴浏览器 User-Agent" className={`${INPUT_CLS} flex-1`} />
                  <button type="button" onClick={genRandomUa} title="随机生成浏览器 User-Agent" className="h-9 px-2.5 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 transition-all flex items-center gap-1.5 shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 active:scale-[0.98]">
                    <Shuffle className="h-3 w-3" /> 随机
                  </button>
                </div>
              )}
            </>
          )}
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-card border border-border bg-surface">
        {/* 高级参数 (折叠) */}
        <div>
          <button type="button" onClick={() => setAdvancedOpen(v => !v)} aria-expanded={advancedOpen}
            className="flex w-full items-center gap-2 px-5 py-3.5 text-left transition-colors hover:bg-elevated/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/60">
            <SectionHeader label="高级参数" inline />
            <ChevronDown className={`ml-auto h-3.5 w-3.5 text-muted transition-transform duration-200 ${advancedOpen ? 'rotate-180' : ''}`} />
          </button>
          {advancedOpen && (
            <div className="grid grid-cols-1 gap-4 px-5 pb-4 md:grid-cols-3">
              <Field label="输出上限 max_tokens" hint="任务请求会被钳制到此值; 默认 16384">
                <input type="number" min={1} value={maxOutputTokens} onChange={e => setMaxOutputTokens(e.target.value)} placeholder="16384" className={INPUT_CLS} />
              </Field>
              <Field label="上下文窗口 (输入上限)" hint="超出时报错提示调大; 默认 128000">
                <input type="number" min={1} value={contextWindow} onChange={e => setContextWindow(e.target.value)} placeholder="128000" className={INPUT_CLS} />
              </Field>
              <Field label="助手工具轮数检查点" hint="连续 N 轮未完成时询问; 0 = 不检查; 默认 100">
                <input type="number" min={0} value={roundCheckpoint} onChange={e => setRoundCheckpoint(e.target.value)} placeholder="100" className={INPUT_CLS} />
              </Field>
            </div>
          )}
        </div>
      </section>

      {/* 操作卡: 安全说明 + 保存/清空 + 配置文件入口。
          独立成卡让「保存」始终在视觉收尾处, 不必滚过长表单才找到。 */}
      <section className="overflow-hidden rounded-card border border-border bg-surface">
        <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center">
          <div className="flex min-w-0 items-start gap-2 sm:flex-1">
            <Shield className="mt-px h-3.5 w-3.5 shrink-0 text-muted/70" />
            <p className="text-[11px] leading-relaxed text-muted">
              {isCodexProvider
                ? 'Codex CLI 会复用本机已登录账户, 分析上下文将发送给 OpenAI/Codex。'
                : 'API Key 仅保存在本机项目文件, 不会上传到任何服务器。'}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            {testResult && (
              <span title="测试只验证当前表单, 不会保存配置; 通过后请点「保存配置」生效"
                className={`inline-flex max-w-[280px] items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] ${testResult.ok ? 'border-bear/25 bg-bear/[0.06] text-bear' : 'border-danger/25 bg-danger/[0.06] text-danger'}`}>
                <span className={`h-1 w-1 shrink-0 rounded-full ${testResult.ok ? 'bg-bear' : 'bg-danger'}`} />
                <span className="min-w-0 truncate">{testResult.msg}</span>
              </span>
            )}
            {configured && (
              <button onClick={() => setConfirmClear(true)} disabled={clear.isPending} className="h-9 px-3.5 rounded-lg text-secondary hover:bg-danger/10 hover:text-danger text-sm flex items-center justify-center gap-1.5 disabled:opacity-50 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface active:scale-[0.98]" title="Clear AI provider configuration">
                <Trash2 className="h-3.5 w-3.5" />
                清空
              </button>
            )}
            <button onClick={handleTest} disabled={testing || !canTest} className="h-9 px-3.5 rounded-lg text-secondary hover:bg-elevated hover:text-foreground text-sm flex items-center justify-center gap-1.5 disabled:opacity-50 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface active:scale-[0.98]" title="测试当前表单的连接, 不保存配置">
              {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wifi className="h-3.5 w-3.5" />}
              {testing ? '测试中' : '测试连接'}
            </button>
            <button onClick={() => save.mutate()} disabled={save.isPending || !canSave} className="h-9 px-5 rounded-lg bg-accent text-white text-sm font-medium flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface active:scale-[0.98]">
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
              {save.isPending ? '保存中...' : saved ? '已保存' : '保存配置'}
            </button>
          </div>
        </div>

        {/* 配置文件: Key 统一存放位置, 支持外部编辑后热加载 */}
        {s?.secrets_path && (
          <div className="border-t border-border/40 px-5 py-3" title="AI 与数据源 Key 统一存放在此文件; 外部编辑保存后点「重新加载」即生效, 无需重启。">
            <div className="flex items-center gap-2">
              <FileCog className="h-3.5 w-3.5 shrink-0 text-muted/70" />
              <span className="shrink-0 text-[11px] text-muted">配置文件</span>
              <code className="min-w-0 truncate rounded bg-base/70 px-1.5 py-0.5 font-mono text-[11px] text-secondary" title={s.secrets_path}>
                {s.secrets_path}
              </code>
              <span className="ml-auto flex shrink-0 items-center gap-1">
                <button type="button" onClick={copySecretsPath}
                  className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-muted transition-colors hover:bg-elevated hover:text-secondary">
                  {pathCopied ? <Check className="h-3 w-3 text-bear" /> : null}
                  {pathCopied ? '已复制' : '复制路径'}
                </button>
                <button type="button" onClick={reloadFromFile} disabled={settings.isFetching}
                  className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] text-muted transition-colors hover:bg-elevated hover:text-secondary disabled:opacity-50">
                  {reloaded ? <Check className="h-3 w-3 text-bear" /> : <RefreshCw className={`h-3 w-3 ${settings.isFetching ? 'animate-spin' : ''}`} />}
                  {reloaded ? '已同步' : '重新加载'}
                </button>
              </span>
            </div>
            <p className="mt-1.5 pl-[22px] text-[10px] leading-relaxed text-muted/80">
              可直接用编辑器修改本文件, 保存后点「重新加载」同步; 所有 AI 与数据源 Key 均在此统一管理。
            </p>
          </div>
        )}
      </section>
      </div>
      </AnchorWrap>

      {confirmClear && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setConfirmClear(false)} />
          <div className="relative w-[90vw] max-w-[380px] rounded-card border border-border bg-base shadow-2xl p-6">
            <h3 className="text-sm font-medium text-foreground mb-2">清空 AI 配置</h3>
            <p className="text-xs text-secondary mb-5 leading-relaxed">
              这会清空已保存的 provider、API Key、API 地址、模型和 Codex CLI 命令。之后可以重新配置。
            </p>
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => setConfirmClear(false)} className="px-3 py-1.5 rounded-btn bg-elevated text-secondary hover:bg-elevated/80 text-sm transition-colors">
                取消
              </button>
              <button onClick={() => clear.mutate()} disabled={clear.isPending} className="px-3 py-1.5 rounded-btn bg-danger/15 text-danger hover:bg-danger/25 text-sm font-medium transition-colors disabled:opacity-50">
                {clear.isPending ? '清空中...' : '确认'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ===== 分区小标题 =====

function SectionHeader({ label, inline }: { label: string; inline?: boolean }) {
  return (
    <div className={`${inline ? '' : 'mb-3'} text-[10px] font-medium uppercase tracking-[0.14em] text-muted`}>
      {label}
    </div>
  )
}

// ===== 表单字段(统一 label + 输入框样式) =====

function Field({ label, hint, inline, children }: {
  label: string
  hint?: string
  inline?: boolean
  children: React.ReactNode
}) {
  if (inline) {
    return (
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] font-medium text-muted">{label}</div>
          {hint && <div className="text-[10px] text-muted/70 mt-0.5">{hint}</div>}
        </div>
        {children}
      </div>
    )
  }
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-medium tracking-wide text-muted">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted/80">{hint}</div>}
    </div>
  )
}

// ===== 开关 =====

function Toggle({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <button
      type="button"
      onClick={onChange}
      className={`relative inline-flex h-5 w-9 items-center rounded-full shrink-0 transition-colors duration-200 ${checked ? 'bg-accent' : 'bg-elevated'}`}
      aria-pressed={checked}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200 ${checked ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  )
}
