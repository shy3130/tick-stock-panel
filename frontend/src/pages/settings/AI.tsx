import { useEffect, useMemo, useState, type ComponentType, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check, Eye, EyeOff, Loader2, Plug, Plus, Save, Settings2,
  Shield, Star, Terminal, Trash2, Wifi, WifiOff, Zap,
} from 'lucide-react'
import { api, type AiProfileInput, type AiProfileMasked, type AiProviderKind } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const INPUT_CLS =
  'w-full h-9 px-2.5 rounded-lg bg-base border-0 ring-1 ring-border/30 text-xs font-mono text-foreground placeholder:text-muted/30 focus:outline-none focus:ring-2 focus:ring-accent/30 transition-shadow'

const OPENAI_PROVIDER = 'openai_compat'
const ACP_PROVIDER = 'acp'
const CODEX_PROVIDER = 'codex_cli'
const CODEX_COMMAND = 'codex'

const EMPTY_FORM: AiProfileInput = {
  name: '',
  provider: OPENAI_PROVIDER,
  base_url: '',
  api_key: '',
  model: '',
  codex_command: CODEX_COMMAND,
  launch_command: '',
  user_agent: '',
}

const PRESETS: Array<AiProfileInput & { label: string; website?: string; websiteLabel?: string; description: string; partner?: boolean; promo?: string }> = [
  { label: 'DeepSeek', name: 'DeepSeek', provider: OPENAI_PROVIDER, base_url: 'https://api.deepseek.com', model: 'deepseek-v4-pro', description: 'DeepSeek 官方 OpenAI 兼容接口。', website: 'https://www.deepseek.com/', websiteLabel: 'deepseek.com' },
  { label: '通义千问', name: '通义千问', provider: OPENAI_PROVIDER, base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-3.6plus', description: '阿里云 DashScope 兼容模式接口。', website: 'https://tongyi.aliyun.com/', websiteLabel: 'tongyi.aliyun.com' },
  { label: '智谱 GLM', name: '智谱 GLM', provider: OPENAI_PROVIDER, base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.2', description: '智谱 AI 官方 OpenAI 兼容接口。', website: 'https://open.bigmodel.cn/', websiteLabel: 'open.bigmodel.cn' },
  { label: 'Kimi', name: 'Kimi', provider: OPENAI_PROVIDER, base_url: 'https://api.moonshot.cn/v1', model: 'kimi-k2.6', description: '月之暗面 Moonshot 官方 OpenAI 兼容接口。', website: 'https://platform.moonshot.cn/', websiteLabel: 'platform.moonshot.cn' },
  { label: 'Hermes (ACP)', name: 'Hermes ACP', provider: ACP_PROVIDER, launch_command: 'hermes acp', model: '', description: '通过本机 Hermes ACP 作为纯文本生成器。' },
  { label: 'Codex CLI', name: 'Codex CLI', provider: CODEX_PROVIDER, codex_command: CODEX_COMMAND, model: '', description: '调用本机 Codex CLI 的 codex exec。', website: 'https://developers.openai.com/codex/noninteractive', websiteLabel: 'codex exec' },
  { label: '炸鸡中转站', name: '炸鸡中转站', provider: OPENAI_PROVIDER, base_url: 'https://code.alysc.top/v1', model: 'gpt-5.5', description: 'OpenAI 兼容中转服务，适合直接使用国际模型。', website: 'https://code.alysc.top/sign-up?aff=1afk', websiteLabel: 'code.alysc.top', partner: true, promo: '通过链接邀请注册赠送免费额度 · 国际模型最低0.01倍率' },
]

function formFromProfile(profile: AiProfileMasked): AiProfileInput {
  return {
    name: profile.name,
    provider: profile.provider,
    base_url: profile.base_url ?? '',
    api_key: '',
    model: profile.model ?? '',
    codex_command: profile.codex_command ?? CODEX_COMMAND,
    launch_command: profile.launch_command ?? '',
    user_agent: profile.user_agent ?? '',
  }
}

export function SettingsAIPanel() {
  const qc = useQueryClient()
  const profilesQuery = useQuery({ queryKey: ['aiProfiles'], queryFn: api.aiProfiles, retry: false })
  const profiles = profilesQuery.data?.profiles ?? []
  const defaultId = profilesQuery.data?.default_id ?? ''
  const [editingId, setEditingId] = useState<string | null>(null)
  const [form, setForm] = useState<AiProfileInput>(EMPTY_FORM)
  const [showKey, setShowKey] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  const editing = useMemo(
    () => profiles.find(profile => profile.id === editingId) ?? null,
    [profiles, editingId],
  )
  const isOpenAI = form.provider === OPENAI_PROVIDER
  const isACP = form.provider === ACP_PROVIDER
  const isCodex = form.provider === CODEX_PROVIDER
  const canSave = form.name.trim() && (isOpenAI ? !!form.model?.trim() : true)
  const configured = profiles.length > 0

  useEffect(() => {
    if (!editingId && profiles[0]) {
      setEditingId(profiles[0].id)
      setForm(formFromProfile(profiles[0]))
    }
  }, [editingId, profiles])

  const refreshProfiles = () => {
    qc.invalidateQueries({ queryKey: ['aiProfiles'] })
    qc.invalidateQueries({ queryKey: QK.settings })
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload = { ...form, name: form.name.trim() }
      if (!payload.api_key) delete payload.api_key
      if (editingId) return api.updateAiProfile(editingId, payload)
      return api.createAiProfile(payload)
    },
    onSuccess: result => {
      const newId = 'id' in result ? result.id : editingId
      if (newId) setEditingId(newId)
      setForm(prev => ({ ...prev, api_key: '' }))
      setSaved(true)
      refreshProfiles()
      setTimeout(() => setSaved(false), 1600)
    },
  })

  const setDefault = useMutation({
    mutationFn: (id: string) => api.setDefaultAiProfile(id),
    onSuccess: refreshProfiles,
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteAiProfile(id),
    onSuccess: (_, id) => {
      if (editingId === id) {
        setEditingId(null)
        setForm(EMPTY_FORM)
      }
      refreshProfiles()
    },
  })

  const test = useMutation({
    mutationFn: async () => {
      if (!editingId) return { ok: false, error: '请先保存配置后再测试' }
      return api.testAiProfile(editingId)
    },
    onSuccess: result => {
      setTestResult({ ok: result.ok, msg: result.ok ? `连通成功 · ${result.model ?? form.name}` : (result.error ?? '测试失败') })
    },
    onError: error => {
      setTestResult({ ok: false, msg: error instanceof Error ? error.message : '测试失败' })
    },
  })

  const applyPreset = (preset: typeof PRESETS[number]) => {
    setEditingId(null)
    setTestResult(null)
    setForm({
      ...EMPTY_FORM,
      name: preset.name,
      provider: preset.provider,
      base_url: preset.base_url ?? '',
      model: preset.model ?? '',
      codex_command: preset.codex_command ?? CODEX_COMMAND,
      launch_command: preset.launch_command ?? '',
    })
  }

  const selectProfile = (profile: AiProfileMasked) => {
    setEditingId(profile.id)
    setTestResult(null)
    setForm(formFromProfile(profile))
  }

  return (
    <div className="space-y-5 max-w-4xl">
      <Card icon={Plug} title="连接状态" right={
        editingId && (
          <button onClick={() => test.mutate()} disabled={test.isPending}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-btn bg-elevated hover:bg-elevated/80 text-xs text-secondary transition-colors duration-150 ease-smooth disabled:opacity-50">
            {test.isPending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wifi className="h-3 w-3" />}
            {test.isPending ? '测试中' : '测试当前'}
          </button>
        )
      }>
        <div className="flex items-center gap-3">
          <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${configured ? 'bg-emerald-400/10 text-emerald-400' : 'bg-amber-400/10 text-amber-400'}`}>
            {configured ? <Wifi className="h-4.5 w-4.5" /> : <WifiOff className="h-4.5 w-4.5" />}
          </div>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">{configured ? `${profiles.length} 个 AI 配置` : 'AI 未配置'}</div>
            <div className="text-xs text-muted mt-0.5 truncate">
              {configured ? `默认: ${profiles.find(p => p.id === defaultId)?.name ?? '未设置'}` : '新增第一条 AI 配置后即可使用分析功能。'}
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
          {PRESETS.map(preset => (
            <button key={preset.label} onClick={() => applyPreset(preset)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-left text-secondary transition-all hover:border-accent/30 hover:text-accent">
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <span>{preset.label}</span>
                {preset.provider === CODEX_PROVIDER && <Terminal className="h-3 w-3" />}
                {preset.partner && <span className="rounded-full border border-orange-400/30 bg-orange-400/10 px-1.5 py-px text-[9px] text-orange-400">赞助</span>}
              </div>
            </button>
          ))}
        </div>
      </Card>

      <div className="grid gap-5 lg:grid-cols-[260px,1fr]">
        <Card icon={Settings2} title="配置列表" right={
          <button onClick={() => { setEditingId(null); setForm(EMPTY_FORM); setTestResult(null) }}
            className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-1 text-xs text-secondary hover:text-accent">
            <Plus className="h-3 w-3" />新增
          </button>
        }>
          <div className="space-y-2">
            {profilesQuery.isLoading && <div className="text-xs text-muted">加载中...</div>}
            {!profilesQuery.isLoading && profiles.length === 0 && <div className="text-xs text-muted">暂无配置。</div>}
            {profiles.map(profile => (
              <button key={profile.id} onClick={() => selectProfile(profile)}
                className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${editingId === profile.id ? 'border-accent/50 bg-accent/10' : 'border-border bg-base hover:border-accent/30'}`}>
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-xs font-medium text-foreground">{profile.name}</div>
                    <div className="mt-0.5 truncate text-[10px] text-muted">{providerLabel(profile.provider)} · {profile.model || profile.launch_command || profile.codex_command || '默认'}</div>
                  </div>
                  {profile.is_default && <Star className="h-3.5 w-3.5 shrink-0 text-amber-400" />}
                </div>
                {profile.available === false && <div className="mt-1 text-[10px] text-amber-400">未检测到宿主机命令</div>}
              </button>
            ))}
          </div>
        </Card>

        <Card icon={Settings2} title={editingId ? '编辑配置' : '新增配置'} right={
          editingId && (
            <div className="flex items-center gap-2">
              {editingId !== defaultId && (
                <button onClick={() => setDefault.mutate(editingId)} disabled={setDefault.isPending}
                  className="inline-flex items-center gap-1 rounded-btn bg-elevated px-2 py-1 text-xs text-secondary hover:text-accent disabled:opacity-50">
                  <Star className="h-3 w-3" />设默认
                </button>
              )}
              <button onClick={() => remove.mutate(editingId)} disabled={remove.isPending}
                className="inline-flex items-center gap-1 rounded-btn bg-danger/10 px-2 py-1 text-xs text-danger hover:bg-danger/20 disabled:opacity-50">
                <Trash2 className="h-3 w-3" />删除
              </button>
            </div>
          )
        }>
          <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="名称">
                <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="例如 DeepSeek / Hermes" className={INPUT_CLS} />
              </Field>
              <Field label="Provider">
                <select value={form.provider} onChange={e => setForm({ ...EMPTY_FORM, name: form.name, provider: e.target.value as AiProviderKind })} className={INPUT_CLS}>
                  <option value={OPENAI_PROVIDER}>OpenAI 兼容</option>
                  <option value={ACP_PROVIDER}>ACP</option>
                  <option value={CODEX_PROVIDER}>Codex CLI</option>
                </select>
              </Field>
            </div>

            {isOpenAI && (
              <>
                <div className="grid gap-4 md:grid-cols-2">
                  <Field label="API 地址">
                    <input value={form.base_url ?? ''} onChange={e => setForm({ ...form, base_url: e.target.value })} placeholder="https://api.example.com/v1" className={INPUT_CLS} />
                  </Field>
                  <Field label="模型">
                    <input value={form.model ?? ''} onChange={e => setForm({ ...form, model: e.target.value })} placeholder="gpt-5.5" className={INPUT_CLS} />
                  </Field>
                </div>
                <Field label="API Key">
                  <div className="relative">
                    <input type={showKey ? 'text' : 'password'} value={form.api_key ?? ''} onChange={e => setForm({ ...form, api_key: e.target.value })} placeholder={editing?.has_api_key ? `${editing.api_key_masked} · 留空不修改` : 'sk-...'} className={`${INPUT_CLS} pr-9`} />
                    <button onClick={() => setShowKey(v => !v)} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted/40 hover:text-muted" tabIndex={-1} aria-label={showKey ? '隐藏' : '显示'}>
                      {showKey ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                </Field>
              </>
            )}

            {isACP && (
              <div className="grid gap-4 md:grid-cols-2">
                <Field label="启动命令">
                  <input value={form.launch_command ?? ''} onChange={e => setForm({ ...form, launch_command: e.target.value })} placeholder="hermes acp" className={INPUT_CLS} />
                </Field>
                <Field label="模型（可选）">
                  <input value={form.model ?? ''} onChange={e => setForm({ ...form, model: e.target.value })} placeholder="留空使用 agent 默认模型" className={INPUT_CLS} />
                </Field>
              </div>
            )}

            {isCodex && (
              <div className="grid gap-4 md:grid-cols-2">
                <Field label="CLI 命令" hint="固定使用默认 codex 命令。">
                  <div className={`${INPUT_CLS} flex items-center text-muted/80 select-none`}>{CODEX_COMMAND}</div>
                </Field>
                <Field label="模型（可选）">
                  <input value={form.model ?? ''} onChange={e => setForm({ ...form, model: e.target.value })} placeholder="留空使用 Codex 默认模型" className={INPUT_CLS} />
                </Field>
              </div>
            )}

            <Field label="User-Agent（可选）">
              <input value={form.user_agent ?? ''} onChange={e => setForm({ ...form, user_agent: e.target.value })} placeholder="默认留空" className={INPUT_CLS} />
            </Field>

            <div className="rounded-card border border-amber-400/20 bg-amber-400/[0.04] px-4 py-3 flex items-start gap-3">
              <Shield className="h-4 w-4 text-amber-400/70 mt-0.5 shrink-0" />
              <div className="text-[11px] text-amber-400/70 leading-relaxed">
                API Key 仅保存在本机项目文件中。ACP/Codex 命令在后端宿主机执行，panel 只把它们作为文本生成器使用。
              </div>
            </div>

            <button onClick={() => save.mutate()} disabled={save.isPending || !canSave}
              className="h-10 w-full rounded-xl bg-accent text-white text-sm font-semibold flex items-center justify-center gap-2 hover:bg-accent/90 disabled:opacity-40 transition-all">
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : saved ? <Check className="h-4 w-4" /> : <Save className="h-4 w-4" />}
              {save.isPending ? '保存中...' : saved ? '已保存' : '保存配置'}
            </button>
          </div>
        </Card>
      </div>
    </div>
  )
}

function providerLabel(provider: string) {
  if (provider === ACP_PROVIDER) return 'ACP'
  if (provider === CODEX_PROVIDER) return 'Codex CLI'
  return 'OpenAI 兼容'
}

interface CardProps {
  icon: ComponentType<{ className?: string }>
  title: string
  right?: ReactNode
  children: ReactNode
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

function Field({ label, hint, children }: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] text-muted/50 uppercase tracking-wider">{label}</div>
      {children}
      {hint && <div className="text-[10px] text-muted">{hint}</div>}
    </div>
  )
}
