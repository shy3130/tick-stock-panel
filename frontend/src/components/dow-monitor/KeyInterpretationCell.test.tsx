import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { KeyInterpretation } from './keyInterpretation'
import { KeyInterpretationCell } from './KeyInterpretationCell'

function fixture(
  overrides: Partial<KeyInterpretation> = {},
): KeyInterpretation {
  return {
    scenarioId: 'BREAKOUT_CONFIRMED',
    category: 'OPPORTUNITY',
    phase: 'CONFIRMED',
    headline: '放量突破已确认',
    explanation: '买盘主动抬价，量能与短周期方向形成共振',
    levels: [
      {
        label: '确认 5m收',
        comparator: '>',
        price: 650.2,
        basis: 'RANGE_60M',
      },
      {
        label: '失效 5m收',
        comparator: '<',
        price: 650.2,
        basis: 'RANGE_60M',
      },
      {
        label: '日高',
        price: 652,
        basis: 'LIVE_DAY_HIGH',
      },
    ],
    dimensions: ['PRICE_STRUCTURE', 'VOLUME', 'FUNDS'],
    accessibleText: '机会，放量突破已确认。买盘主动抬价，量能与短周期方向形成共振。确认5分钟收盘高于650.20，失效5分钟收盘低于650.20，日高652.00',
    ...overrides,
  }
}

describe('KeyInterpretationCell', () => {
  it('renders conclusion, business explanation, and named prices as three lines', () => {
    render(<KeyInterpretationCell interpretation={fixture()} />)

    const root = screen.getByTestId('key-interpretation')
    expect(root).toHaveAccessibleName(/重点解读，机会，放量突破已确认/)
    expect(screen.getByText('放量突破已确认')).toBeInTheDocument()
    expect(screen.getByText('买盘主动抬价，量能与短周期方向形成共振'))
      .toBeInTheDocument()
    expect(screen.getByText(/确认 5m收>650\.20/)).toBeInTheDocument()
    expect(screen.getByText(/失效 5m收<650\.20/)).toBeInTheDocument()
    expect(root.querySelectorAll('[data-interpretation-line]')).toHaveLength(3)
  })

  it('colors only the category or risk phrase and never the whole cell', () => {
    render(<KeyInterpretationCell interpretation={fixture({
      scenarioId: 'DOWNSIDE_ACCELERATION',
      category: 'RISK',
      phase: 'ATTEMPT',
      headline: '下跌正在加速',
      explanation: '价格逼近日低，主动卖盘与放量方向一致',
      accessibleText: '风险，下跌正在加速。价格逼近日低，主动卖盘与放量方向一致。',
    })} />)

    const root = screen.getByTestId('key-interpretation')
    expect(screen.getByText('风险')).toHaveClass('text-danger')
    expect(screen.getByText('下跌正在加速')).toHaveClass('text-danger')
    expect(root).not.toHaveClass('text-danger')
    expect(root.className).not.toMatch(/bg-danger|bg-red/)
  })

  it('shows a deterministic fallback when no reliable price is available', () => {
    render(<KeyInterpretationCell interpretation={fixture({
      scenarioId: 'DATA_UNAVAILABLE',
      category: 'DATA',
      phase: 'NONE',
      headline: '关键数据延迟',
      explanation: '行情或K线时效不足，暂停实时机会判断',
      levels: [],
      dimensions: [],
      accessibleText: '数据，关键数据延迟。行情或K线时效不足，暂停实时机会判断。关键价待确认',
    })} />)

    expect(screen.getByText('关键价待确认')).toBeInTheDocument()
  })
})
