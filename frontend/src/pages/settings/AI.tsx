import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Save, Loader2, Check, Wifi, WifiOff, Eye, EyeOff, Shield,
  Shuffle, Plug, Zap, Settings2, ExternalLink, Trash2,
  Terminal,
} from 'lucide-react'
import { useSettings } from '@/lib/useSharedQueries'
import { api, type SettingsState } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

// 统一的输入框样式(与项目其他设置页一致)
const INPUT_CLS =
  'w-full h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/30 text-xs font-mono text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow'

const CODEX_PROVIDER = 'codex_cli'
const OPENAI_PROVIDER = 'openai_compat'
const CODEX_COMMAND = 'codex'
const DEFAULT_CODEX_MODEL = 'gpt-5.6-sol'
const DEFAULT_CODEX_REASONING_EFFORT = 'xhigh'
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

const PRESETS: { label: string; provider?: string; url: string; model: string; codexCommand?: string; website: string; websiteLabel: string; description: string; custom?: boolean }[] = [
  { label: '自定义', url: '', model: '', website: '', websiteLabel: '', description: '不自动填充任何配置，完全手动填写 API 地址、模型和密钥。', custom: true },
  { label: 'DeepSeek', url: 'https://api.deepseek.com', model: 'deepseek-v4-pro', website: 'https://www.deepseek.com/', websiteLabel: 'deepseek.com', description: 'DeepSeek 官方 OpenAI 兼容接口。' },
  { label: '通义千问', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-3.6plus', website: 'https://tongyi.aliyun.com/', websiteLabel: 'tongyi.aliyun.com', description: '阿里云 DashScope 兼容模式接口。' },
  { label: '智谱 GLM', url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2', website: 'https://open.bigmodel.cn/', websiteLabel: 'open.bigmodel.cn', description: '智谱 AI 官方 OpenAI 兼容接口。' },
  { label: 'Kimi', url: 'https://api.moonshot.cn/v1', model: 'kimi-k2.7-code', website: 'https://platform.moonshot.cn/', websiteLabel: 'platform.moonshot.cn', description: '月之暗面 Moonshot 官方 OpenAI 兼容接口，支持超长上下文。' },
  { label: 'Codex CLI', provider: CODEX_PROVIDER, url: '', model: DEFAULT_CODEX_MODEL, codexCommand: CODEX_COMMAND, website: 'https://developers.openai.com/codex/noninteractive', websiteLabel: 'codex exec', description: '调用本机 Codex CLI 的 codex exec, 适合已登录 ChatGPT/Codex 的本地环境。' },
  { label: '炸鸡中转站', url: 'https://api.zhaji.dev/v1', model: 'gpt-5.5', website: 'https://api.zhaji.dev', websiteLabel: 'api.zhaji.dev', description: 'OpenAI 兼容中转服务，适合直接使用国际模型。' },
  { label: 'OrcaRouter', url: 'https://api.orcarouter.ai/v1', model: 'orcarouter/auto', website: 'https://www.orcarouter.ai', websiteLabel: 'orcarouter.ai', description: 'OrcaRouter 官方 OpenAI 兼容网关，聚合前沿模型并在同一端点提供网关级零信任安全防护。' },
]

export function SettingsAIPanel() {
  const qc = useQueryClient()
  const settings = useSettings()
  const s = settings.data

  const [provider, setProvider] = useState(OPENAI_PROVIDER)
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [codexReasoningEffort, setCodexReasoningEffort] = useState('')
  const [codexCommand, setCodexCommand] = useState(CODEX_COMMAND)
  const [customUa, setCustomUa] = useState(false)
  const [userAgent, setUserAgent] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  const isCodexProvider = provider === CODEX_PROVIDER
  const savedCodexProvider = s?.ai_provider === CODEX_PROVIDER
  const configured = s?.ai_configured ?? (savedCodexProvider ? !!(s?.ai_codex_command ?? CODEX_COMMAND) : s?.has_ai_key)
  // 选中的预设: 精确匹配 provider+url/codexCommand; 匹配不上时默认"自定义"
  const matchedPreset = PRESETS.find(p => (p.provider ?? OPENAI_PROVIDER) === provider && (isCodexProvider ? p.codexCommand === codexCommand : p.url === baseUrl))
  const selectedPreset = matchedPreset ?? PRESETS.find(p => p.custom)
  const savedCodexModel = savedCodexProvider ? (s?.ai_model ?? '') : ''
  const savedCodexEffort = savedCodexProvider ? (s?.ai_codex_reasoning_effort ?? '') : ''
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
    option.model === model && option.effort === codexReasoningEffort,
  ) ?? CODEX_MODEL_OPTIONS[0]
  const codexModelSelectValue = selectedCodexModelOption.value
  const canSave = isCodexProvider ? true : !!baseUrl.trim() && !!model.trim()

  useEffect(() => {
    if (!s) return
    // 未配置过 AI (无 api_key): 字段留空, 默认选中"自定义"预设, 不预填充后端默认值
    const unconfigured = !s.has_ai_key && !s.ai_configured
    const savedProvider = s.ai_provider ?? OPENAI_PROVIDER
    setProvider(savedProvider)
    setBaseUrl(unconfigured ? '' : (s.ai_base_url ?? ''))
    setModel(unconfigured ? '' : (s.ai_model ?? ''))
    setCodexReasoningEffort(unconfigured ? '' : (s.ai_codex_reasoning_effort ?? ''))
    setCodexCommand(s.ai_codex_command ?? CODEX_COMMAND)
    const ua = s.ai_user_agent ?? ''
    setCustomUa(!!ua)
    setUserAgent(ua)
  }, [s])

  const payload = () => ({
    provider,
    base_url: baseUrl,
    api_key: apiKey || undefined,
    model,
    codex_command: isCodexProvider ? CODEX_COMMAND : codexCommand,
    codex_reasoning_effort: isCodexProvider ? codexReasoningEffort : '',
    user_agent: customUa ? userAgent : '',
  })

  const save = useMutation({
    mutationFn: () => api.saveAiSettings(payload()),
    onSuccess: (result) => {
      setSaved(true)
      setApiKey('')
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: result.ai_provider ?? provider,
        ai_base_url: baseUrl,
        ai_model: result.ai_model ?? model,
        ai_codex_command: result.ai_codex_command ?? (isCodexProvider ? CODEX_COMMAND : codexCommand),
        ai_codex_reasoning_effort: result.ai_codex_reasoning_effort ?? (isCodexProvider ? codexReasoningEffort : ''),
        ai_configured: result.ai_configured ?? (isCodexProvider ? true : (apiKey ? true : prev.ai_configured)),
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
      setProvider(OPENAI_PROVIDER)
      setBaseUrl('')
      setApiKey('')
      setModel('')
      setCodexReasoningEffort('')
      setCodexCommand(CODEX_COMMAND)
      setTestResult(null)
      qc.setQueryData<SettingsState>(QK.settings, prev => prev ? {
        ...prev,
        ai_provider: OPENAI_PROVIDER,
        ai_base_url: '',
        ai_model: '',
        ai_codex_command: CODEX_COMMAND,
        ai_codex_reasoning_effort: '',
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

  const handlePreset = (p: typeof PRESETS[number]) => {
    if (p.custom) {
      // 自定义: 清空所有自动填充字段, 由用户完全手动填写
      setProvider(OPENAI_PROVIDER)
      setBaseUrl('')
      setModel('')
      setCodexReasoningEffort('')
      return
    }
    setProvider(p.provider ?? OPENAI_PROVIDER)
    setBaseUrl(p.url)
    setModel(p.model)
    setCodexReasoningEffort(p.provider === CODEX_PROVIDER ? DEFAULT_CODEX_REASONING_EFFORT : '')
    if (p.codexCommand) setCodexCommand(CODEX_COMMAND)
  }

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      if (canSave) await api.saveAiSettings(payload())
      const r = await api.strategyAiTest()
      setTestResult({ ok: r.ok, msg: r.ok ? `连通成功 · ${r.model ?? provider}` : (r.error ?? '未知错误') })
    } catch (e: any) {
      setTestResult({ ok: false, msg: String(e?.message ?? '测试失败') })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="space-y-5 max-w-2xl">
      <Card icon={Plug} title="连接状态" right={
        configured && (
          <button onClick={handleTest} disabled={testing}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn bg-elevated hover:bg-elevated/80 text-xs text-secondary transition-colors duration-150 ease-smooth disabled:opacity-50">
            {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
            {testing ? '测试中' : '测试'}
          </button>
        )
      }>
        <div className="flex items-center gap-3">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${configured ? 'bg-emerald-400/10 text-emerald-400' : 'bg-amber-400/10 text-amber-400'}`}>
            {configured ? <Wifi className="h-4.5 w-4.5" /> : <WifiOff className="h-4.5 w-4.5" />}
          </div>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">{configured ? 'AI 已连接' : 'AI 未配置'}</div>
            <div className="text-xs text-muted mt-0.5 truncate">
              {configured
                ? (savedCodexProvider
                  ? `${s?.ai_codex_command ?? CODEX_COMMAND} · ${codexModelLabel(s?.ai_model, s?.ai_codex_reasoning_effort)}`
                  : `${s?.ai_model} · ${s?.ai_api_key_masked}`)
                : (isCodexProvider ? '使用本机 codex exec, 此处无需填写 API Key。' : '配置 API Key 后即可使用 AI 功能。')}
            </div>
          </div>
        </div>
        {testResult && (
          <div className={`mt-3 rounded-btn border px-3 py-2 text-xs flex items-center gap-2 ${testResult.ok ? 'border-emerald-400/20 bg-emerald-400/[0.04] text-emerald-400' : 'border-danger/20 bg-danger/[0.04] text-danger'}`}>
            <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${testResult.ok ? 'bg-emerald-400' : 'bg-danger'}`} />
            {testResult.msg}
          </div>
        )}
      </Card>

      <Card icon={Zap} title="快速预设">
        <div className="flex flex-wrap items-start gap-2">
          {PRESETS.map(p => (
            <button key={p.label} onClick={() => handlePreset(p)}
              className={`rounded-lg border px-3 py-2 text-left transition-all ${selectedPreset?.label === p.label ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-base text-secondary hover:border-accent/30'}`}>
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <span>{p.label}</span>
                {p.provider === CODEX_PROVIDER && <Terminal className="h-3 w-3" />}
              </div>
            </button>
          ))}
        </div>
        {selectedPreset && (
          <div className="mt-3 rounded-btn border border-border/30 bg-base/30 px-3 py-2 text-[11px] leading-relaxed">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-secondary">{selectedPreset.description}</span>
            </div>
            {selectedPreset.website && (
              <a href={selectedPreset.website} target="_blank" rel="noreferrer"
                className="mt-1 inline-flex items-center gap-1 text-muted hover:text-accent transition-colors">
                {selectedPreset.websiteLabel}
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        )}
      </Card>

      <Card
        icon={Settings2}
        title="自定义配置"
        right={
          <span className="inline-flex items-center gap-1.5 text-[10px] text-muted/60" title={isCodexProvider ? 'Use local Codex CLI via codex exec' : 'Use OpenAI-compatible Chat Completions API'}>
            <span className="rounded-full border border-border/40 bg-base/50 px-1.5 py-px font-mono">{isCodexProvider ? 'codex exec' : 'Chat Completions'}</span>
            {isCodexProvider ? 'CLI' : '接口'}
          </span>
        }
      >
        <div className="space-y-4">
          {isCodexProvider ? (
            <div className="grid grid-cols-2 gap-4">
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
                    setModel(option.model)
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
              <div className="grid grid-cols-2 gap-4">
                <Field label="API 地址">
                  <input type="text" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder="https://api.zhaji.dev/v1" className={INPUT_CLS} />
                </Field>
                <Field label="模型">
                  <input type="text" value={model} onChange={e => setModel(e.target.value)} placeholder="gpt-5.6-sol" className={INPUT_CLS} />
                </Field>
              </div>

              <Field label="API Key">
                <div className="flex gap-2">
                  <div className="flex-1 relative">
                    <input type={showKey ? 'text' : 'password'} value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={configured ? `${s?.ai_api_key_masked} · 留空不修改` : 'sk-...'} className={`${INPUT_CLS} pr-9`} />
                    <button onClick={() => setShowKey(v => !v)} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted/40 hover:text-muted" tabIndex={-1} aria-label={showKey ? '隐藏' : '显示'}>
                      {showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                  <button onClick={handleTest} disabled={testing || !apiKey} className="h-9 px-3 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 disabled:opacity-40 transition-all flex items-center gap-1.5 shrink-0">
                    {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
                    测试
                  </button>
                </div>
              </Field>

              <div className="border-t border-border/20" />

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Field label="自定义 User-Agent" inline>
                    <Toggle checked={customUa} onChange={() => setCustomUa(v => !v)} />
                  </Field>
                </div>
                {customUa && (
                  <div className="flex gap-2">
                    <input type="text" value={userAgent} onChange={e => setUserAgent(e.target.value)} placeholder="粘贴浏览器 User-Agent" className={`${INPUT_CLS} flex-1`} />
                    <button type="button" onClick={genRandomUa} title="随机生成浏览器 User-Agent" className="h-9 px-2.5 rounded-lg border border-border/50 text-xs text-secondary hover:text-accent hover:border-accent/30 transition-all flex items-center gap-1.5 shrink-0">
                      <Shuffle className="h-3 w-3" /> 随机
                    </button>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </Card>

      <div className="rounded-card border border-amber-400/20 bg-amber-400/[0.04] px-4 py-3 flex items-start gap-3">
        <Shield className="h-4 w-4 text-amber-400/70 mt-0.5 shrink-0" />
        <div className="text-[11px] text-amber-400/70 leading-relaxed">
          {isCodexProvider
            ? 'Codex CLI 模式会复用本机已登录的 Codex 账户, 个股、财务、复盘等分析上下文会发送给 OpenAI/Codex。保存即表示确认仅在本机或可信内网使用。'
            : 'API Key 仅保存在本机项目文件中, 不会上传到任何服务器。请妥善保管。'}
        </div>
      </div>

      <div className="flex gap-2">
        <button onClick={() => save.mutate()} disabled={save.isPending || !canSave} className="flex-1 h-10 rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all">
          {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
          {save.isPending ? '保存中...' : saved ? '已保存' : '保存配置'}
        </button>
        {configured && (
          <button onClick={() => setConfirmClear(true)} disabled={clear.isPending} className="h-10 px-4 rounded-xl bg-elevated text-secondary hover:text-danger text-sm flex items-center justify-center gap-1.5 hover:bg-elevated/80 disabled:opacity-50 transition-all shrink-0" title="Clear AI provider configuration">
            <Trash2 className="h-4 w-4" />
            清空
          </button>
        )}
      </div>

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

// ===== 通用卡片(与 Keys 页风格统一) =====

interface CardProps {
  icon: React.ComponentType<{ className?: string }>
  title: string
  right?: React.ReactNode
  children: React.ReactNode
}

function Card({ icon: Icon, title, right, children }: CardProps) {
  return (
    <section className="rounded-card border border-border bg-surface p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <Icon className="h-4 w-4 text-secondary" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
        </div>
        {right}
      </div>
      {children}
    </section>
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
          <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
          {hint && <div className="text-[10px] text-muted mt-0.5">{hint}</div>}
        </div>
        {children}
      </div>
    )
  }
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted">{hint}</div>}
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
