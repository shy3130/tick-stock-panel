import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { StockCard } from './Watchlist'

describe('Watchlist card', () => {
  it('renders decimal change_pct as percentage points', () => {
    const html = renderToStaticMarkup(React.createElement(StockCard, {
      r: {
        symbol: '300169.SZ',
        name: '天晟新材',
        close: 5.38,
        change_pct: 0.2,
        turnover_rate: 17.38,
        vol_ratio_5d: 7.5,
        rsi_14: 63.7,
      },
      candleRows: [],
      showCandle: false,
      onPreview: vi.fn(),
      onConfirmRemove: vi.fn(),
      onCancelRemove: vi.fn(),
      onRequestRemove: vi.fn(),
      isConfirming: false,
      extCols: [],
      expandedCells: new Set<string>(),
      onToggleExpand: vi.fn(),
      onDimensionClick: vi.fn(),
      isMonitored: false,
    }))

    expect(html).toContain('+20.00%')
    expect(html).not.toContain('+0.20%')
  })
})
