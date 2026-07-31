import { cn } from '@/lib/cn'

import {
  formatInterpretationPrice,
  type InterpretationCategory,
  type KeyInterpretation,
} from './keyInterpretation'

const CATEGORY_LABEL: Record<InterpretationCategory, string> = {
  OPPORTUNITY: '机会',
  RISK: '风险',
  ANOMALY: '异动',
  OBSERVE: '观察',
  DATA: '数据',
}

const CATEGORY_CLASS: Record<InterpretationCategory, string> = {
  OPPORTUNITY: 'border-danger/35 bg-danger/10 text-danger',
  RISK: 'border-danger/35 bg-danger/10 text-danger',
  ANOMALY: 'border-amber-400/35 bg-amber-400/10 text-amber-300',
  OBSERVE: 'border-border bg-elevated text-muted',
  DATA: 'border-border bg-elevated text-muted',
}

function levelText(
  label: string,
  comparator: '>' | '<' | undefined,
  price: number,
): string {
  return `${label}${comparator ?? ''}${formatInterpretationPrice(price)}`
}

export function KeyInterpretationCell({
  interpretation,
  compact = false,
  testId = 'key-interpretation',
}: {
  interpretation: KeyInterpretation
  compact?: boolean
  testId?: string
}) {
  const category = CATEGORY_LABEL[interpretation.category]
  const risk = interpretation.category === 'RISK'
  return (
    <div
      data-testid={testId}
      className={cn(
        'grid gap-1 leading-tight text-foreground',
        compact ? 'min-w-0' : 'min-w-[320px]',
      )}
      aria-label={`重点解读，${interpretation.accessibleText}`}
      title={interpretation.accessibleText}
    >
      <div
        data-interpretation-line="conclusion"
        className={cn(
          'flex min-w-0 items-start gap-1.5 text-[11px] font-semibold',
          !compact && 'items-center whitespace-nowrap',
        )}
      >
        <span
          className={cn(
            'inline-flex shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-semibold',
            CATEGORY_CLASS[interpretation.category],
          )}
        >
          {category}
        </span>
        <span
          className={cn(
            'min-w-0',
            compact ? 'whitespace-normal' : 'overflow-hidden text-ellipsis',
            risk && 'text-danger',
          )}
        >
          {interpretation.headline}
        </span>
      </div>
      <div
        data-interpretation-line="explanation"
        className={cn(
          'text-[10px] text-foreground/80',
          compact ? 'whitespace-normal' : 'overflow-hidden text-ellipsis whitespace-nowrap',
        )}
      >
        {interpretation.explanation}
      </div>
      <div
        data-interpretation-line="levels"
        className={cn(
          'font-mono text-[9px] text-muted',
          compact ? 'whitespace-normal' : 'overflow-hidden text-ellipsis whitespace-nowrap',
        )}
      >
        {interpretation.levels.length === 0
          ? '关键价待确认'
          : interpretation.levels.map((level, index) => (
              <span key={`${level.basis}-${level.label}-${level.price}`}>
                {index > 0 && <span aria-hidden="true">｜</span>}
                <span className={cn(index === 0 && risk && 'font-semibold text-danger')}>
                  {levelText(level.label, level.comparator, level.price)}
                </span>
              </span>
            ))}
      </div>
    </div>
  )
}
