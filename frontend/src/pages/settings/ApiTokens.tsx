import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, KeyRound, Plus } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

/** 对外开放 scope 清单 (与后端 api_tokens.SCOPES 同步) */
const SCOPES: { key: string; label: string; desc: string; default: boolean }[] = [
  { key: 'read:market', label: '行情读取', desc: '标的搜索 / 日K / 分时 / 指数 / 市场快照', default: true },
  { key: 'read:ext', label: '扩展数据读取', desc: '扩展表 rows / values / schema 查询', default: false },
  { key: 'write:ext', label: '扩展数据写入', desc: '向已配置的扩展表程序化写入行数据 (会进入策略/回测数据面, 谨慎授予)', default: false },
  { key: 'read:analysis', label: '分析结果读取', desc: '策略清单与结果 / 回测报告 / 市场环境 / 告警', default: false },
  { key: 'run:backtest', label: '触发回测', desc: '提交回测 / 选股 / 因子检验任务 (受并发约束)', default: false },
  { key: 'paper:trade', label: '模拟盘交易', desc: '模拟盘读取与下单/撤单 (写操作, 最高敏感)', default: false },
]

export function SettingsApiTokensPanel() {
  const qc = useQueryClient()
  const tokensQ = useQuery({ queryKey: QK.apiTokens, queryFn: api.apiTokensList })
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<string[]>(['read:market'])
  const [plaintext, setPlaintext] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')

  const createM = useMutation({
    mutationFn: () => api.apiTokenCreate(name, scopes),
    onSuccess: r => {
      setPlaintext(r.plaintext)
      setCreating(false)
      setName('')
      setScopes(['read:market'])
      setError('')
      qc.invalidateQueries({ queryKey: QK.apiTokens })
    },
    onError: e => setError(String((e as Error).message)),
  })

  const revokeM = useMutation({
    mutationFn: (id: string) => api.apiTokenRevoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.apiTokens }),
  })

  const tokens = (tokensQ.data?.tokens ?? []).filter(t => !t.revoked)

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="rounded-card border border-border bg-surface p-4">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-accent" />
          <div className="text-sm font-medium">API Token</div>
          <span className="text-[10px] text-muted">外部调用方的第二认证通道 — 与面板密码并行, 互不影响</span>
          <button
            onClick={() => setCreating(v => !v)}
            className="ml-auto flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent"
          >
            <Plus className="h-3 w-3" /> 新建 Token
          </button>
        </div>

        {/* 新建表单 */}
        {creating && (
          <div className="mt-3 space-y-3 rounded-card border border-border/60 bg-base/60 p-3">
            <div>
              <label className="text-[11px] text-muted">名称 (用途备注)</label>
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="如: 我的行情看板"
                className="mt-1 w-full rounded-btn border border-border bg-base px-3 py-1.5 text-sm outline-none focus:border-accent/50"
              />
            </div>
            <div className="space-y-1.5">
              <div className="text-[11px] text-muted">权限 (scope)</div>
              {SCOPES.map(s => (
                <label key={s.key} className="flex cursor-pointer items-start gap-2 rounded-btn px-2 py-1.5 hover:bg-elevated/50">
                  <input
                    type="checkbox"
                    checked={scopes.includes(s.key)}
                    onChange={e => setScopes(prev => e.target.checked ? [...prev, s.key] : prev.filter(x => x !== s.key))}
                    className="mt-0.5 h-3.5 w-3.5 shrink-0"
                  />
                  <span className="min-w-0">
                    <span className="text-xs text-foreground">
                      {s.label}
                      <span className="ml-1.5 font-mono text-[10px] text-accent/80">{s.key}</span>
                      {s.default && <span className="ml-1 rounded bg-accent/10 px-1 text-[9px] text-accent">默认</span>}
                    </span>
                    <span className="block text-[10px] text-muted">{s.desc}</span>
                  </span>
                </label>
              ))}
              <div className="text-[10px] text-muted">
                扩展表的结构配置/上传/拉取、数据同步、设置等管理接口永不开放给 Token (行数据写入走 write:ext)。调用方式: <span className="font-mono">Authorization: Bearer tsp_...</span>, 默认限流 120 次/分钟, 契约文档 <span className="font-mono text-accent/80">/api/openapi.json?tier=a</span>
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => name.trim() && scopes.length > 0 && createM.mutate()}
                disabled={!name.trim() || scopes.length === 0 || createM.isPending}
                className="rounded-btn bg-accent px-4 py-1.5 text-xs font-medium text-white transition-opacity hover:bg-accent/90 disabled:opacity-50"
              >
                {createM.isPending ? '创建中…' : '创建'}
              </button>
              <button onClick={() => setCreating(false)} className="rounded-btn border border-border px-4 py-1.5 text-xs text-secondary transition-colors hover:text-foreground">取消</button>
            </div>
            {error && <div className="rounded-btn bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{error}</div>}
          </div>
        )}

        {/* 明文一次性展示 */}
        {plaintext && (
          <div className="mt-3 rounded-card border border-warning/40 bg-warning/10 p-3">
            <div className="text-xs font-medium text-warning">Token 已创建 — 明文只显示这一次, 请立即保存</div>
            <div className="mt-2 flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-btn bg-base px-2 py-1.5 font-mono text-xs">{plaintext}</code>
              <button
                onClick={() => { navigator.clipboard?.writeText(plaintext); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
                className="flex shrink-0 items-center gap-1 rounded-btn border border-border px-2 py-1.5 text-[11px] text-secondary transition-colors hover:text-accent"
              >
                <Copy className="h-3 w-3" /> {copied ? '已复制' : '复制'}
              </button>
              <button onClick={() => setPlaintext(null)} className="shrink-0 rounded-btn border border-border px-2 py-1.5 text-[11px] text-secondary hover:text-foreground">关闭</button>
            </div>
          </div>
        )}

        {/* 列表 */}
        <div className="mt-3">
          {tokensQ.isLoading ? (
            <div className="py-6 text-center text-xs text-muted">加载中…</div>
          ) : tokens.length === 0 ? (
            <div className="py-6 text-center text-xs text-muted">尚无 Token — 创建一个, 外部程序即可按 scope 调用开放接口</div>
          ) : (
            <div className="space-y-1.5">
              {tokens.map(t => (
                <div key={t.id} className="flex items-center gap-2 rounded-btn border border-border/60 bg-base/60 px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-xs text-foreground">
                      {t.name}
                      <span className="ml-1.5 font-mono text-[10px] text-muted">{t.id}</span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1">
                      {t.scopes.map(s => (
                        <span key={s} className="rounded bg-accent/10 px-1.5 py-px font-mono text-[9px] text-accent">{s}</span>
                      ))}
                      <span className="ml-1 text-[9px] text-muted">
                        创建 {t.created_at?.slice(0, 16).replace('T', ' ')}
                        {t.last_used_at ? ` · 最近使用 ${t.last_used_at.slice(11, 16)}` : ' · 未使用过'}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={() => revokeM.mutate(t.id)}
                    disabled={revokeM.isPending}
                    className={cn('shrink-0 rounded-btn border border-border px-2 py-1 text-[10px] text-muted transition-colors hover:border-danger/40 hover:text-danger')}
                  >
                    吊销
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
