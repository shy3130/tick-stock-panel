import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PageHeader } from './PageHeader'

describe('PageHeader responsive structure', () => {
  it('keeps actions in a named horizontally scrollable region', () => {
    render(<PageHeader title="自选股" subtitle="说明" right={<button>刷新</button>} />)
    const actions = screen.getByLabelText('页面操作')
    expect(actions).toHaveClass('overflow-x-auto')
    expect(screen.getByText('说明').parentElement).toHaveClass('flex-wrap')
  })
})
