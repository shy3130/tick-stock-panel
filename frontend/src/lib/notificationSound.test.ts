import { describe, expect, it } from 'vitest'
import { CUSTOM_SOUND_PREFIX, customSoundUrl, isCustomSoundKey, mergeSoundOptions } from './notificationSound'

describe('mergeSoundOptions', () => {
  it('无自定义包时等于内置列表', () => {
    expect(mergeSoundOptions([]).map(o => o.key)).toEqual([
      'ding', 'chime', 'rise', 'fall', 'alert', 'electronic',
      'drop', 'bell', 'pingpong', 'magic', 'soft', 'none',
    ])
  })

  it('自定义包插在合成声效之后, 无声恒排末尾', () => {
    const opts = mergeSoundOptions(['我的提示音.mp3', 'cash.wav'])
    expect(opts[opts.length - 1].key).toBe('none')
    const keys = opts.map(o => o.key)
    expect(keys).toContain(CUSTOM_SOUND_PREFIX + '我的提示音.mp3')
    expect(keys.indexOf(CUSTOM_SOUND_PREFIX + 'cash.wav')).toBeGreaterThan(
      keys.indexOf('soft'),
    )
    expect(keys.indexOf(CUSTOM_SOUND_PREFIX + 'cash.wav')).toBeLessThan(keys.indexOf('none'))
  })

  it('key 与 url 换算互逆且文件名经 URL 编码', () => {
    expect(isCustomSoundKey(CUSTOM_SOUND_PREFIX + 'a.mp3')).toBe(true)
    expect(isCustomSoundKey('ding')).toBe(false)
    expect(customSoundUrl(CUSTOM_SOUND_PREFIX + '我的音 效.mp3')).toBe(
      '/api/settings/sound-packs/' + encodeURIComponent('我的音 效.mp3') + '/file',
    )
  })
})
