import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Settings2 } from 'lucide-react'
import type { CustomSignal } from '@/lib/api'
import { CustomSignalDialog } from './CustomSignalDialog'

interface Props {
  kind: 'entry' | 'exit'
  signals: string[]
  onChange: (next: string[]) => void
  buttonClassName?: string
  iconClassName?: string
}

export function SignalTriggerActions({ kind, signals, onChange, buttonClassName, iconClassName }: Props) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)

  const accent = kind === 'entry' ? 'hover:text-accent hover:border-accent/40' : 'hover:text-warning hover:border-warning/40'
  const btnCls = buttonClassName ?? 'rounded-btn border border-border bg-base p-1 text-muted transition-colors cursor-pointer'
  const iconCls = iconClassName ?? 'h-3.5 w-3.5'

  const handleSaved = (signal: CustomSignal) => {
    if (signal.kind !== kind && signal.kind !== 'both') return
    const signalId = `csg_${signal.id}`
    onChange(signals.includes(signalId) ? signals : [...signals, signalId])
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="新增自定义信号"
        className={`${btnCls} ${accent}`}
      >
        <Plus className={iconCls} />
      </button>
      <button
        type="button"
        onClick={() => navigate('/settings?tab=signals')}
        title="去信号库"
        className={`${btnCls} hover:border-amber-400/40 hover:text-amber-400`}
      >
        <Settings2 className={iconCls} />
      </button>

      <CustomSignalDialog
        open={open}
        defaultKind={kind}
        onClose={() => setOpen(false)}
        onSaved={handleSaved}
      />
    </>
  )
}
