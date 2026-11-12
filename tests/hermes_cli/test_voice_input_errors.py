"""Terminal input failures remain structured through the continuous voice wrapper."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_voice_wrapper(monkeypatch):
    from hermes_cli import voice

    monkeypatch.setattr(voice, "_continuous_active", False)
    monkeypatch.setattr(voice, "_continuous_stopping", False)
    monkeypatch.setattr(voice, "_continuous_recorder", None)
    monkeypatch.setattr(voice, "_continuous_callbacks", voice._NO_CALLBACKS)
    monkeypatch.setattr(voice, "_play_beep", lambda *_a, **_k: None)


def test_start_continuous_reports_provider_open_error(monkeypatch):
    from hermes_cli import voice
    from tools.voice_input import VoiceInputError

    error = VoiceInputError("private pulse unavailable", stage="open", retryable=True)

    class _Recorder:
        on_live_partial = None
        on_input_error = None

        def start(self, **_kwargs):
            raise error

    monkeypatch.setattr(voice, "create_audio_recorder", _Recorder)
    errors = []

    with pytest.raises(VoiceInputError) as raised:
        voice.start_continuous(lambda _text: None, on_input_error=errors.append)

    assert raised.value is error
    assert errors == [error]


def test_start_continuous_wires_asynchronous_provider_errors(monkeypatch):
    from hermes_cli import voice
    from tools.voice_input import VoiceInputEOF

    class _Recorder:
        on_live_partial = None
        on_input_error = None

        def start(self, **_kwargs):
            return None

    recorder = _Recorder()
    monkeypatch.setattr(voice, "create_audio_recorder", lambda: recorder)
    errors = []

    assert voice.start_continuous(lambda _text: None, on_input_error=errors.append) is True
    error = VoiceInputEOF("parec ended")
    recorder.on_input_error(error)

    assert errors == [error]


def test_transcription_failure_is_reported_as_stt_input_error(monkeypatch, tmp_path):
    from hermes_cli import voice

    wav = tmp_path / "capture.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(
        voice,
        "transcribe_recording",
        lambda _path: {"success": False, "error": "cloud STT unavailable"},
    )
    errors = []

    assert voice._transcribe_wav(
        str(wav), "failed: %s", on_input_error=errors.append
    ) is None
    assert len(errors) == 1
    assert errors[0].stage == "stt"
    assert errors[0].retryable is True
    assert "cloud STT unavailable" in str(errors[0])


def test_stop_continuous_propagates_physical_release_failure(monkeypatch):
    from hermes_cli import voice
    from tools.voice_input import VoiceInputReleaseError

    error = VoiceInputReleaseError("parec process did not release")

    class _Recorder:
        def cancel(self):
            raise error

    errors = []
    monkeypatch.setattr(voice, "_continuous_active", True)
    monkeypatch.setattr(voice, "_continuous_recorder", _Recorder())
    monkeypatch.setattr(
        voice,
        "_continuous_callbacks",
        (None, None, None, None, errors.append),
    )

    with pytest.raises(VoiceInputReleaseError):
        voice.stop_continuous()
    assert errors == [error]
