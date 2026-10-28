"""The provider seam preserves the native recorder and full-duplex algorithms."""

from __future__ import annotations

import math


class _Block:
    def __init__(self, values):
        self.values = list(values)

    def copy(self):
        return _Block(self.values)

    def astype(self, _dtype):
        return self

    def __pow__(self, power):
        return _Block(value**power for value in self.values)

    def tobytes(self):
        return b"\0\0" * len(self.values)

    def __len__(self):
        return len(self.values)


class _Numpy:
    float64 = float

    @staticmethod
    def sqrt(value):
        return math.sqrt(value)

    @staticmethod
    def mean(data):
        return sum(data.values) / len(data.values)

    @staticmethod
    def concatenate(blocks, axis=0):
        del axis
        return _Block(value for block in blocks for value in block.values)


np = _Numpy()


class _Stream:
    def __init__(self, *, blocks=None, error=None):
        self.active = False
        self.started = 0
        self.closed = 0
        self._blocks = list(blocks or [])
        self._error = error

    def start(self):
        self.started += 1
        self.active = True
        return self

    def read(self, _frames):
        if self._error is not None:
            raise self._error
        return self._blocks.pop(0), False

    def close(self, timeout=3.0):
        del timeout
        self.closed += 1
        self.active = False


class _Provider:
    def __init__(self, *, exclusive, stream):
        self.requires_exclusive_handoff = exclusive
        self.stream = stream
        self.opened = []

    def preferred_sample_rate(self):
        return 16_000

    def is_available(self):
        return True

    def open_stream(self, **kwargs):
        self.opened.append(kwargs)
        return self.stream


def test_recorder_uses_provider_and_physically_releases_exclusive_input(monkeypatch):
    from tools import voice_input, voice_mode

    stream = _Stream()
    provider = _Provider(exclusive=True, stream=stream)
    monkeypatch.setattr(voice_input, "get_voice_input_provider", lambda **_kw: provider)
    monkeypatch.setattr(voice_mode, "_import_audio", lambda: (object(), np))
    monkeypatch.setattr(voice_mode, "_import_numpy", lambda: np, raising=False)
    monkeypatch.setattr(voice_mode.AudioRecorder, "_open_live_session", lambda *_a: None)
    monkeypatch.setattr(voice_mode.AudioRecorder, "_write_wav", lambda *_a, **_k: "capture.wav")
    recorder = voice_mode.AudioRecorder()

    recorder.start()
    callback = provider.opened[0]["callback"]
    callback(_Block([1_000] * 6_400), 6_400, None, None)
    recorder.stop()

    assert provider.opened[0]["phase"] == "ordinary"
    assert provider.opened[0]["sample_rate"] == 16_000
    assert stream.started == 1
    assert stream.closed == 1


def test_default_nonexclusive_provider_keeps_the_native_persistent_stream(monkeypatch):
    from tools import voice_input, voice_mode

    stream = _Stream()
    provider = _Provider(exclusive=False, stream=stream)
    monkeypatch.setattr(voice_input, "get_voice_input_provider", lambda **_kw: provider)
    monkeypatch.setattr(voice_mode, "_import_audio", lambda: (object(), np))
    monkeypatch.setattr(voice_mode, "_import_numpy", lambda: np, raising=False)
    monkeypatch.setattr(voice_mode.AudioRecorder, "_open_live_session", lambda *_a: None)
    monkeypatch.setattr(voice_mode.AudioRecorder, "_write_wav", lambda *_a, **_k: "capture.wav")
    recorder = voice_mode.AudioRecorder()

    recorder.start()
    provider.opened[0]["callback"](
        _Block([1_000] * 6_400), 6_400, None, None
    )
    recorder.stop()

    assert stream.closed == 0
    assert recorder._stream is stream
    recorder.shutdown()
    assert stream.closed == 1


def test_full_duplex_surfaces_provider_eof_and_releases_stream(monkeypatch):
    from tools import voice_input, voice_mode

    error = voice_input.VoiceInputEOF("parec ended")
    stream = _Stream(error=error)
    provider = _Provider(exclusive=True, stream=stream)
    monkeypatch.setattr(voice_input, "get_voice_input_provider", lambda **_kw: provider)
    monkeypatch.setattr(voice_mode, "_import_numpy", lambda: np, raising=False)
    errors = []

    result = voice_mode.full_duplex_listen(
        lambda: False,
        is_playing=lambda: False,
        on_input_error=errors.append,
    )

    assert result is None
    assert errors == [error]
    assert provider.opened[0]["phase"] == "full_duplex"
    assert stream.closed == 1


def test_pulse_provider_availability_does_not_import_portaudio(monkeypatch):
    from tools import voice_input, voice_mode

    provider = _Provider(exclusive=True, stream=_Stream())
    monkeypatch.setattr(voice_input, "get_voice_input_provider", lambda **_kw: provider)
    monkeypatch.setattr(
        voice_mode,
        "_import_audio",
        lambda: (_ for _ in ()).throw(AssertionError("PortAudio should not be imported")),
    )

    assert voice_mode._audio_available() is True
