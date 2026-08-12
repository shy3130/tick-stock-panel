import { describe, expect, it } from 'vitest'

import { STAGE_LABELS } from './ActiveJobCard'

describe('盘后任务阶段文案', () => {
  it('uses a beginner-readable label for strategy cache verification', () => {
    expect(STAGE_LABELS.refresh_strategies).toBe('重算并校验策略')
  })
})
