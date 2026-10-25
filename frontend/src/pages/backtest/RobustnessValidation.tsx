import { useState } from 'react'
import { ShieldCheck, SlidersHorizontal, Waypoints } from 'lucide-react'
import { StrategyOptimizer } from './StrategyOptimizer'
import { StrategyWalkForward } from './StrategyWalkForward'
import { StrategyGuardian } from './StrategyGuardian'

type Mode = 'sensitivity' | 'walkforward' | 'guardian'

export function RobustnessValidation() {
  const [mode, setMode] = useState<Mode>('sensitivity')
  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex shrink-0 items-center border-b border-border/70 px-1 pb-2">
        <div className="inline-flex rounded-btn border border-border bg-surface/80 p-0.5">
          {([
            ['sensitivity', '稳健优化', SlidersHorizontal],
            ['walkforward', '滚动样本外检验', Waypoints],
            ['guardian', '策略守护', ShieldCheck],
          ] as const).map(([value, label, Icon]) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              className={`inline-flex items-center gap-1.5 rounded-[5px] px-3 py-1.5 text-xs font-medium transition-colors ${mode === value
                ? 'bg-accent text-white shadow-sm'
                : 'text-secondary hover:bg-elevated hover:text-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        {mode === 'sensitivity' && <StrategyOptimizer />}
        {mode === 'walkforward' && <StrategyWalkForward />}
        {mode === 'guardian' && <StrategyGuardian />}
      </div>
    </div>
  )
}
