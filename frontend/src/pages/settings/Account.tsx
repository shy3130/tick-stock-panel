import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, KeyRound, ShieldCheck, UserCog, UserX } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { SettingsPanel, SettingsSection, settingsSecondaryButtonClass } from './SettingsPrimitives'

export function SettingsAccountPanel() {
  const qc = useQueryClient()
  const [label, setLabel] = useState('')
  const [newCode, setNewCode] = useState('')
  const accounts = useQuery({ queryKey: ['admin-users'], queryFn: api.adminUsers })
  const invites = useQuery({ queryKey: ['admin-invites'], queryFn: api.adminInvites })
  const createInvite = useMutation({
    mutationFn: () => api.adminCreateInvite(label),
    onSuccess: ({ code }) => {
      setNewCode(code)
      setLabel('')
      qc.invalidateQueries({ queryKey: ['admin-invites'] })
    },
  })
  const toggleUser = useMutation({
    mutationFn: ({ id, disabled }: { id: string; disabled: boolean }) => api.adminDisableUser(id, disabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-users'] }),
  })

  return (
    <SettingsPanel icon={UserCog} title="账户与邀请码" description="管理工作台成员、登录状态和一次性邀请码。">
      <SettingsSection title="成员">
        <div className="overflow-x-auto rounded-card border border-border">
          <table className="w-full min-w-[34rem] text-left text-xs">
            <thead className="bg-elevated/60 text-[10px] uppercase tracking-wider text-muted">
              <tr><th className="px-3 py-2">用户名</th><th className="px-3 py-2">角色</th><th className="px-3 py-2">状态</th><th className="px-3 py-2 text-right">操作</th></tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(accounts.data?.users ?? []).map((account) => (
                <tr key={account.id} className="text-foreground">
                  <td className="px-3 py-2.5 font-medium">{account.username}</td>
                  <td className="px-3 py-2.5 text-secondary">{account.role === 'admin' ? '管理员' : '成员'}</td>
                  <td className="px-3 py-2.5"><span className={cn('rounded px-1.5 py-0.5 text-[10px]', account.disabled ? 'bg-danger/10 text-danger' : 'bg-bull/10 text-bull')}>{account.disabled ? '已停用' : '正常'}</span></td>
                  <td className="px-3 py-2.5 text-right">
                    <button type="button" disabled={account.role === 'admin' || toggleUser.isPending} onClick={() => toggleUser.mutate({ id: account.id, disabled: !account.disabled })} className={cn(settingsSecondaryButtonClass, 'px-2 py-1 text-[11px] disabled:opacity-40')} title={account.disabled ? '启用账户' : '停用账户'}>
                      <UserX className="h-3.5 w-3.5" />{account.disabled ? '启用' : '停用'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SettingsSection>

      <SettingsSection title="生成邀请码">
        <div className="flex flex-col gap-2 sm:flex-row">
          <input value={label} onChange={e => setLabel(e.target.value)} placeholder="备注（可选）" className="h-9 flex-1 rounded-btn border border-border bg-base px-3 text-xs text-foreground outline-none focus:border-accent/50" />
          <button type="button" onClick={() => createInvite.mutate()} disabled={createInvite.isPending} className="inline-flex h-9 items-center justify-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white disabled:opacity-50"><KeyRound className="h-3.5 w-3.5" />生成一次性邀请码</button>
        </div>
        {newCode && <div className="mt-3 flex items-center justify-between gap-2 rounded-btn border border-accent/30 bg-accent/10 px-3 py-2 font-mono text-sm text-accent"><span>{newCode}</span><button type="button" onClick={() => void navigator.clipboard?.writeText(newCode)} title="复制邀请码" aria-label="复制邀请码" className="rounded p-1 hover:bg-accent/10"><Copy className="h-4 w-4" /></button></div>}
      </SettingsSection>

      <SettingsSection title="邀请码记录">
        <div className="space-y-1.5 text-xs">
          {(invites.data?.invites ?? []).map((invite) => <div key={invite.digest} className="flex items-center justify-between rounded-btn bg-elevated/40 px-3 py-2"><span className="text-secondary">{invite.label || '未备注'}</span><span className={invite.redeemed_by ? 'text-muted' : 'text-bull'}>{invite.redeemed_by ? '已使用' : '待使用'}</span></div>)}
          {!invites.data?.invites?.length && <div className="flex items-center gap-2 text-muted"><ShieldCheck className="h-4 w-4" />尚未生成邀请码</div>}
        </div>
      </SettingsSection>
    </SettingsPanel>
  )
}
