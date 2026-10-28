import { atom } from 'nanostores'

import type { AnyGatewayEvent } from '../gatewayTypes.js'

// Live STT text so far while the user is still speaking (`voice.partial`, stt.streaming). The
// composer shows it as its placeholder until the capture settles.
export const $voicePartial = atom('')

const latestLeaseEpoch = new Map<string, number>()

export function resetVoiceLeaseFenceForTests(): void {
  latestLeaseEpoch.clear()
}

interface VoiceCaptureSetters {
  setProcessing: (value: boolean) => void
  setRecording: (value: boolean) => void
}

/** Capture-state events (`voice.partial`, `voice.status`); true when fully handled here. A
 *  `voice.transcript` only clears the partial and stays with the main handler. */
export function handleVoiceCapture(ev: AnyGatewayEvent, voice: VoiceCaptureSetters): boolean {
  if (ev.type.startsWith('voice.')) {
    const payload = ev.payload as Record<string, unknown> | undefined
    const owner = String(payload?.owner_session_id ?? '')
    const epoch = Number(payload?.lease_epoch ?? 0)

    // Older gateways emitted no scope; keep that native compatibility. Once
    // scoped, reject cross-session or lower-epoch callbacks before they can
    // mutate the composer after a release/reacquire boundary.
    if (owner && Number.isSafeInteger(epoch) && epoch > 0) {
      if (ev.session_id && ev.session_id !== owner) {
        return true
      }

      const latest = latestLeaseEpoch.get(owner) ?? 0

      if (epoch < latest) {
        return true
      }

      latestLeaseEpoch.set(owner, epoch)
    }
  }

  if (ev.type === 'voice.partial') {
    $voicePartial.set(String(ev.payload?.text ?? ''))

    return true
  }

  if (ev.type === 'voice.transcript') {
    $voicePartial.set('')

    return false
  }

  if (ev.type === 'voice.input_error') {
    $voicePartial.set('')
    voice.setRecording(false)
    voice.setProcessing(false)

    return false
  }

  if (ev.type !== 'voice.status') {
    return false
  }

  // The continuous VAD loop reports listening / transcribing / idle so the status bar needs no polling.
  const state = String(ev.payload?.state ?? '')
  voice.setRecording(state === 'listening')
  voice.setProcessing(state === 'transcribing')

  if (state !== 'listening' && state !== 'transcribing') {
    $voicePartial.set('')
  }

  return true
}
