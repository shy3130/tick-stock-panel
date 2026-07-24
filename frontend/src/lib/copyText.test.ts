import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './copyText'

describe('copyText', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    })
    Reflect.deleteProperty(document, 'execCommand')
    document.querySelectorAll('textarea').forEach(element => element.remove())
  })

  it('uses navigator clipboard when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })

    await expect(copyText('# report')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('# report')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('falls back to a temporary readonly textarea when clipboard fails', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    Object.defineProperty(document, 'execCommand', {
      value: execCommand,
      configurable: true,
    })

    await expect(copyText('# report')).resolves.toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('returns false and removes the temporary textarea when both methods fail', async () => {
    const execCommand = vi.fn().mockReturnValue(false)
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    })
    Object.defineProperty(document, 'execCommand', {
      value: execCommand,
      configurable: true,
    })

    await expect(copyText('# report')).resolves.toBe(false)
    expect(document.querySelector('textarea')).toBeNull()
  })
})
