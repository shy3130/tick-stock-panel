import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Save, Loader2, Check, Wifi, WifiOff, Eye, EyeOff, Shield,
  Shuffle, Plug, Zap, Settings2, ExternalLink, Trash2,
  Terminal, Sparkles,
} from 'lucide-react'
import { useSettings } from '@/lib/useSharedQueries'
import { api, type SettingsState } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import {
  SettingsPanel,
  SettingsSection,
  SettingsToggleRow,
  settingsControlClass,
  settingsPrimaryButtonClass,
  settingsSecondaryButtonClass,
} from './SettingsPrimitives'

const INPUT_CLS = `${settingsControlClass} font-mono`

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

type CodexModelOption = {
  label: string
  value: string
  model: string
  effort: string
  hint: string
}

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
  description: string
}

const PRESETS: AiPreset[] = [
  { label: 'DeepSeek', url: 'https://api.deepseek.com/v1', model: 'deepseek-v4-pro', website: 'https://www.deepseek.com/', websiteLabel: 'deepseek.com', description: 'DeepSeek 官方 OpenAI 兼容接口。' },
  { label: 'GLM', url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2', website: 'https://open.bigmodel.cn/', websiteLabel: 'open.bigmodel.cn', description: '智谱 AI 官方 OpenAI 兼容接口。' },
  { label: 'Kimi', url: 'https://api.moonshot.cn/v1', model: 'kimi-k2.6', website: 'https://platform.moonshot.cn/', websiteLabel: 'platform.moonshot.cn', description: '月之暗面 Moonshot 官方 OpenAI 兼容接口，支持超长上下文。' },
  { label: 'Qwen', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3.7-plus', website: 'https://tongyi.aliyun.com/', websiteLabel: 'tongyi.aliyun.com', description: '阿里云 DashScope OpenAI 兼容接口。' },
  { label: 'MiMo', url: 'https://api.xiaomimimo.com/v1', model: 'mimo-v2.5-pro', website: 'https://platform.xiaomimimo.com/', websiteLabel: 'platform.xiaomimimo.com', description: 'Xiaomi MiMo 官方 OpenAI 兼容接口。' },
]

const CODEX_PRESET: AiPreset = {
  label: 'Codex CLI',
  provider: CODEX_PROVIDER,
  url: '',
  model: DEFAULT_CODEX_MODEL,
  codexCommand: CODEX_COMMAND,
  website: 'https://developers.openai.com/codex/noninteractive',
  websiteLabel: 'codex exec',
  description: '调用本机 Codex CLI 的 codex exec。',
}

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
  const selectedPreset = !isCodexProvider
    ? PRESETS.find(p => p.url === baseUrl && p.model === model)
    : undefined
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
  const requiresNewApiKey = !isCodexProvider
    && !!s?.has_ai_key
    && baseUrl.trim().replace(/\/+$/, '') !== (s?.ai_base_url ?? '').trim().replace(/\/+$/, '')
  const canSave = isCodexProvider
    ? true
    : !!baseUrl.trim() && !!model.trim() && (!requiresNewApiKey || !!apiKey.trim())

  useEffect(() => {
    if (!s) return
    const unconfigured = !s.has_ai_key && !s.ai_configured
    setProvider(s.ai_provider ?? OPENAI_PROVIDER)
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

  const handlePreset = (p: AiPreset) => {
    setProvider(p.provider ?? OPENAI_PROVIDER)
    setBaseUrl(p.url)
    setModel(p.model)
    setCodexReasoningEffort(p.provider === CODEX_PROVIDER ? DEFAULT_CODEX_REASONING_EFFORT : '')
    setTestResult(null)
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
    <SettingsPanel
      icon={Sparkles}
      title="AI 设置"
      description="连接模型服务，为策略分析、财务解读与复盘提供智能能力。"
      width="narrow"
    >
      <SettingsSection icon={Plug} title="连接状态" action={
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
      </SettingsSection>

      <SettingsSection icon={Zap} title="快速预设">
        <div className="flex flex-wrap items-start gap-2">
          {PRESETS.map(p => (
            <button type="button" key={p.label} onClick={() => handlePreset(p)}
              className={`rounded-lg border px-3 py-2 text-left transition-all ${selectedPreset?.label === p.label ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-base text-secondary hover:border-accent/30'}`}>
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <span>{p.label}</span>
              </div>
            </button>
          ))}
        </div>
        {selectedPreset && (
          <div className="mt-3 rounded-btn border border-border/30 bg-base/30 px-3 py-2 text-[11px] leading-relaxed">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-secondary">{selectedPreset.description}</span>
            </div>
            <a href={selectedPreset.website} target="_blank" rel="noreferrer"
              className="mt-1 inline-flex items-center gap-1 text-muted hover:text-accent transition-colors">
              {selectedPreset.websiteLabel}
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
        )}
      </SettingsSection>

      <SettingsSection
        icon={Settings2}
        title="自定义配置"
        action={
          <button
            type="button"
            onClick={() => handlePreset(isCodexProvider ? PRESETS[0] : CODEX_PRESET)}
            className={cn(settingsSecondaryButtonClass, 'h-8 px-2.5')}
          >
            {isCodexProvider ? <Plug className="h-3.5 w-3.5" /> : <Terminal className="h-3.5 w-3.5" />}
            {isCodexProvider ? 'API 模式' : 'Codex CLI'}
          </button>
        }
      >
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
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <Field label="API 地址">
                  <input type="text" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" className={INPUT_CLS} />
                </Field>
                <Field label="模型">
                  <input type="text" value={model} onChange={e => setModel(e.target.value)} placeholder="gpt-5.5" className={INPUT_CLS} />
                </Field>
              </div>

              <Field
                label="API Key"
                hint={requiresNewApiKey ? '已切换模型服务，请输入该服务对应的新 API Key。' : undefined}
              >
                <div className="flex gap-2">
                  <div className="flex-1 relative">
                    <input
                      type={showKey ? 'text' : 'password'}
                      value={apiKey}
                      onChange={e => setApiKey(e.target.value)}
                      placeholder={requiresNewApiKey ? '请输入新服务的 API Key' : configured ? `${s?.ai_api_key_masked} · 留空不修改` : 'sk-...'}
                      className={`${INPUT_CLS} pr-9`}
                    />
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
                <SettingsToggleRow
                  label="自定义 User-Agent"
                  description="为模型请求设置自定义 User-Agent。"
                  checked={customUa}
                  onCheckedChange={setCustomUa}
                />
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
      </SettingsSection>

      <div className="rounded-card border border-amber-400/20 bg-amber-400/[0.04] px-4 py-3 flex items-start gap-3">
        <Shield className="h-4 w-4 text-amber-400/70 mt-0.5 shrink-0" />
        <div className="text-[11px] text-amber-400/70 leading-relaxed">
          {isCodexProvider
            ? 'Codex CLI 模式会复用本机已登录的 Codex 账户, 个股、财务、复盘等分析上下文会发送给 OpenAI/Codex。保存即表示确认仅在本机或可信内网使用。'
            : 'API Key 仅保存在本机项目文件中, 不会上传到任何服务器。请妥善保管。'}
        </div>
      </div>

      <div className="flex gap-2">
        <button type="button" onClick={() => save.mutate()} disabled={save.isPending || !canSave} className={cn(settingsPrimaryButtonClass, 'flex-1')}>
          {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
          {save.isPending ? '保存中...' : saved ? '已保存' : '保存配置'}
        </button>
        {configured && (
          <button type="button" onClick={() => setConfirmClear(true)} disabled={clear.isPending} className={cn(settingsSecondaryButtonClass, 'shrink-0 hover:text-danger')} title="Clear AI provider configuration">
            <Trash2 className="h-4 w-4" />
            清空
          </button>
        )}
      </div>

      <ConfirmDialog
        open={confirmClear}
        title="清空 AI 配置"
        message="这会清空已保存的 provider、API Key、API 地址、模型和 Codex CLI 命令。之后可以重新配置。"
        confirmText="确认清空"
        danger
        pending={clear.isPending}
        onCancel={() => setConfirmClear(false)}
        onConfirm={() => clear.mutate()}
      />
    </SettingsPanel>
  )
}

// ===== 表单字段(统一 label + 输入框样式) =====

function Field({ label, hint, children }: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[11px] font-medium text-secondary">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted">{hint}</div>}
    </div>
  )
}
