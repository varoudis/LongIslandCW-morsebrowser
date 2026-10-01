/* global SpeechSynthesisVoice */
/** Respect explicit choices online; offline requires a browser-reported local voice. */
export function selectPracticeVoice (
  selected: SpeechSynthesisVoice | null,
  voices: SpeechSynthesisVoice[],
  offline: boolean
): SpeechSynthesisVoice | null {
  if (selected && (!offline || selected.localService)) return selected
  const local = voices.filter(voice => voice.localService)
  const preferred = local.find(voice => /^en[-_]/i.test(voice.lang)) || local[0]
  return preferred || (offline ? null : voices[0] || null)
}
