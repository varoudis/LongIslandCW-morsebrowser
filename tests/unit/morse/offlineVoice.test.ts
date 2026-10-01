import { afterEach, describe, expect, it, vi } from 'vitest'
import { selectPracticeVoice } from '../../../src/morse/voice/voiceAvailability'
import { MorseVoice } from '../../../src/morse/voice/MorseVoice'
import { MorseVoiceInfo } from '../../../src/morse/voice/MorseVoiceInfo'
import EasySpeech from 'easy-speech'

vi.mock('easy-speech', () => ({ default: {
  detect: () => ({ speechSynthesis: true, speechSynthesisUtterance: true }),
  speak: vi.fn(() => new Promise(() => {})),
  cancel: vi.fn()
} }))
const remote = { name: 'Remote English', lang: 'en-US', localService: false } as SpeechSynthesisVoice
const local = { name: 'Local English', lang: 'en-GB', localService: true } as SpeechSynthesisVoice

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.clearAllMocks() })

describe('optional offline speech', () => {
  it('prefers local voices but respects an explicit online choice', () => {
    expect(selectPracticeVoice(null, [remote, local], false)).toBe(local)
    expect(selectPracticeVoice(remote, [remote, local], false)).toBe(remote)
    expect(selectPracticeVoice(remote, [remote, local], true)).toBe(local)
    expect(selectPracticeVoice(remote, [remote], true)).toBeNull()
  })
  it('continues Morse immediately when only remote voices are available offline', () => {
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    const voice = new MorseVoice({ logToFlaggedWords: vi.fn() } as any)
    voice.voices = [remote] as any
    const info = Object.assign(new MorseVoiceInfo(), { textToSpeak: 'test', voice: remote, rate: 1, onEnd: vi.fn() })
    voice.speakInfo(info)
    expect(info.onEnd).toHaveBeenCalledOnce()
    expect(EasySpeech.speak).not.toHaveBeenCalled()
  })
  it('continues Morse once when the speech engine never signals completion', () => {
    vi.useFakeTimers()
    const voice = new MorseVoice({ logToFlaggedWords: vi.fn() } as any)
    voice.voices = [local] as any
    const info = Object.assign(new MorseVoiceInfo(), { textToSpeak: 'test', voice: local, rate: 1, onEnd: vi.fn() })
    voice.speakInfo(info)
    vi.advanceTimersByTime(15000)
    expect(info.onEnd).toHaveBeenCalledOnce()
    expect(EasySpeech.cancel).toHaveBeenCalledOnce()
    vi.advanceTimersByTime(120000)
    expect(info.onEnd).toHaveBeenCalledOnce()
  })
  it('clears an old watchdog on Stop so it cannot cancel resumed speech', () => {
    vi.useFakeTimers()
    const voice = new MorseVoice({ logToFlaggedWords: vi.fn() } as any)
    voice.voices = [local] as any
    const first = Object.assign(new MorseVoiceInfo(), { textToSpeak: 'one', voice: local, rate: 1, onEnd: vi.fn() })
    const second = Object.assign(new MorseVoiceInfo(), { textToSpeak: 'two', voice: local, rate: 1, onEnd: vi.fn() })
    voice.speakInfo(first)
    vi.advanceTimersByTime(10000)
    voice.cancelSpeech()
    vi.advanceTimersByTime(25)
    voice.speakInfo(second)
    vi.advanceTimersByTime(5000)
    expect(first.onEnd).not.toHaveBeenCalled()
    expect(second.onEnd).not.toHaveBeenCalled()
    expect(EasySpeech.cancel).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(10000)
    expect(second.onEnd).toHaveBeenCalledOnce()
    expect(EasySpeech.cancel).toHaveBeenCalledTimes(3)
  })

})
