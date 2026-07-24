import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Key, Radio } from 'lucide-react'
import { describe, expect, it, vi } from 'vitest'
import { SettingsTabs } from './SettingsTabs'

describe('SettingsTabs', () => {
  it('exposes the selected tab and reports a new selection', async () => {
    const onChange = vi.fn()
    const user = userEvent.setup()
    render(
      <SettingsTabs
        tabs={[
          { key: 'account', label: 'TickFlow', icon: Key },
          { key: 'monitoring', label: '实时监控', icon: Radio },
        ]}
        activeKey="account"
        onChange={onChange}
      />,
    )
    expect(screen.getByRole('tab', { name: 'TickFlow' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: '实时监控' }))
    expect(onChange).toHaveBeenCalledWith('monitoring')
  })
})
