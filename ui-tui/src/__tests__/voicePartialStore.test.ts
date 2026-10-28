import { describe, expect, it, vi } from 'vitest'

import { $voicePartial, handleVoiceCapture, resetVoiceLeaseFenceForTests } from '../app/voicePartialStore.js'

describe('handleVoiceCapture', () => {
  it('drops stale epochs for the same scoped owner', () => {
    resetVoiceLeaseFenceForTests()
    const voice = { setProcessing: vi.fn(), setRecording: vi.fn() }

    handleVoiceCapture({
      payload: { lease_epoch: 8, owner_session_id: 'sid', text: 'new' },
      session_id: 'sid',
      type: 'voice.partial'
    } as any, voice)
    handleVoiceCapture({
      payload: { lease_epoch: 7, owner_session_id: 'sid', text: 'stale' },
      session_id: 'sid',
      type: 'voice.partial'
    } as any, voice)

    expect($voicePartial.get()).toBe('new')
  })

  it('clears capture state on a scoped input failure', () => {
    resetVoiceLeaseFenceForTests()
    const voice = { setProcessing: vi.fn(), setRecording: vi.fn() }
    $voicePartial.set('unfinished')

    const handled = handleVoiceCapture({
      payload: { lease_epoch: 1, message: 'microphone EOF', owner_session_id: 'sid' },
      session_id: 'sid',
      type: 'voice.input_error'
    } as any, voice)

    expect(handled).toBe(false)
    expect($voicePartial.get()).toBe('')
    expect(voice.setRecording).toHaveBeenLastCalledWith(false)
    expect(voice.setProcessing).toHaveBeenLastCalledWith(false)
  })

  it('shows live STT text until the capture settles', () => {
    resetVoiceLeaseFenceForTests()
    const voice = { setProcessing: vi.fn(), setRecording: vi.fn() }

    handleVoiceCapture({ payload: { text: 'schedule a meeting' }, type: 'voice.partial' } as any, voice)
    handleVoiceCapture({ payload: { state: 'transcribing' }, type: 'voice.status' } as any, voice)
    expect($voicePartial.get()).toBe('schedule a meeting')
    expect(voice.setProcessing).toHaveBeenLastCalledWith(true)

    handleVoiceCapture({ payload: { state: 'idle' }, type: 'voice.status' } as any, voice)
    expect($voicePartial.get()).toBe('')
    expect(voice.setRecording).toHaveBeenLastCalledWith(false)
  })
})
