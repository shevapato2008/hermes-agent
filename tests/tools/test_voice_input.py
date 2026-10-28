"""Focused contracts for process-scoped microphone input providers."""

from __future__ import annotations

import os
import struct
import subprocess
import threading

import pytest


class _ChunkedStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, chunks):
        self.stdout = _ChunkedStdout(chunks)
        self.returncode = None
        self.terminated = 0
        self.waited = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def wait(self, timeout=None):
        self.waited += 1
        return self.returncode or 0

    def kill(self):
        self.killed += 1
        self.returncode = -9


def test_default_provider_is_portaudio_and_keeps_the_native_input_rate():
    from tools.voice_input import PortAudioVoiceInputProvider, create_voice_input_provider

    fake_sd = type(
        "FakeSoundDevice",
        (),
        {"query_devices": staticmethod(lambda *_a, **_k: {"default_samplerate": 48_000.0})},
    )()
    provider = create_voice_input_provider({}, import_audio=lambda: (fake_sd, object()))

    assert isinstance(provider, PortAudioVoiceInputProvider)
    assert provider.preferred_sample_rate() == 48_000
    assert provider.requires_exclusive_handoff is False


def test_canonical_nested_config_selects_private_pulse_cli_provider():
    from tools.voice_input import PulseCliVoiceInputProvider, create_voice_input_provider

    provider = create_voice_input_provider({
        "voice": {
            "input": {
                "provider": "pulse_cli",
                "pulse_cli": {
                    "server": "unix:/run/smartbox/pulse/native",
                    "source": "echo-cancel-source",
                    "executable": "smartbox-parec",
                },
            }
        }
    })

    assert isinstance(provider, PulseCliVoiceInputProvider)
    assert provider.pulse_server == "unix:/run/smartbox/pulse/native"
    assert provider.pulse_source == "echo-cancel-source"
    assert provider.executable == "smartbox-parec"
    assert provider.requires_exclusive_handoff is True


def test_pulse_cli_is_mono_16k_s16le_scrubs_credentials_and_reaps(monkeypatch):
    from tools.voice_input import PulseCliVoiceInputProvider

    pcm = struct.pack("<4h", 1, -2, 300, -400)
    process = _FakeProcess([pcm[:3], pcm[3:5], pcm[5:]])
    opened = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    def fake_popen(argv, **kwargs):
        opened.append((argv, kwargs))
        return process

    monkeypatch.setattr("tools.voice_input.subprocess.Popen", fake_popen)
    provider = PulseCliVoiceInputProvider(
        pulse_server="unix:/run/smartbox/pulse/native",
        pulse_source="echo-cancel-source",
        executable="parec",
    )
    stream = provider.open_stream(
        phase="ordinary", sample_rate=16_000, channels=1, dtype="int16", blocksize=4
    )

    data, overflowed = stream.read(4)
    stream.close(timeout=0.2)

    assert data.reshape(-1).tolist() == [1, -2, 300, -400]
    assert overflowed is False
    argv, kwargs = opened[0]
    assert argv == [
        "parec", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
        "--device=echo-cancel-source",
    ]
    assert kwargs["env"]["PULSE_SERVER"] == "unix:/run/smartbox/pulse/native"
    assert kwargs["env"]["PULSE_SOURCE"] == "echo-cancel-source"
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert process.terminated == 1 and process.waited == 1
    assert process.stdout.closed is True


def test_pulse_cli_eof_is_terminal(monkeypatch):
    from tools.voice_input import PulseCliVoiceInputProvider, VoiceInputEOF

    process = _FakeProcess([b""])
    monkeypatch.setattr("tools.voice_input.subprocess.Popen", lambda *_a, **_k: process)
    stream = PulseCliVoiceInputProvider(executable="parec").open_stream(
        phase="full_duplex", sample_rate=16_000, channels=1, dtype="int16", blocksize=480
    )

    with pytest.raises(VoiceInputEOF) as first:
        stream.read(480)
    assert first.value.stage == "read" and first.value.retryable is False
    with pytest.raises(VoiceInputEOF):
        stream.read(480)
    stream.close(timeout=0.2)


def test_pulse_pump_error_callback_can_close_through_helper_thread(monkeypatch):
    """The pump must exit before dispatching an error that synchronously tears it down."""
    from tools.voice_input import PulseCliVoiceInputProvider

    process = _FakeProcess([b""])
    monkeypatch.setattr("tools.voice_input.subprocess.Popen", lambda *_a, **_k: process)
    provider = PulseCliVoiceInputProvider(executable="parec")
    callback_done = threading.Event()
    close_errors = []
    callback_threads = []
    stream = None

    def on_error(_error):
        callback_threads.append(threading.current_thread().name)

        def close_from_helper():
            try:
                stream.close(timeout=0.1)
            except BaseException as exc:
                close_errors.append(exc)

        helper = threading.Thread(target=close_from_helper)
        helper.start()
        helper.join(timeout=2)
        if helper.is_alive():
            close_errors.append(AssertionError("voice input close helper did not finish"))
        callback_done.set()

    stream = provider.open_stream(
        phase="ordinary",
        sample_rate=16_000,
        channels=1,
        dtype="int16",
        blocksize=480,
        callback=lambda *_args: None,
        on_error=on_error,
    )
    stream.start()
    assert callback_done.wait(2)
    try:
        assert close_errors == []
        assert callback_threads != ["hermes-pulse-input"]
        assert stream.active is False
        assert process.terminated == 1 and process.waited == 1
    finally:
        provider.release_active(timeout=1)


def test_pulse_cli_allows_only_one_capture_phase(monkeypatch):
    from tools.voice_input import PulseCliVoiceInputProvider, VoiceInputBusyError

    processes = []

    def fake_popen(*_args, **_kwargs):
        process = _FakeProcess([b""])
        processes.append(process)
        return process

    monkeypatch.setattr("tools.voice_input.subprocess.Popen", fake_popen)
    provider = PulseCliVoiceInputProvider(executable="parec")
    ordinary = provider.open_stream(
        phase="ordinary", sample_rate=16_000, channels=1, dtype="int16", blocksize=480
    )
    with pytest.raises(VoiceInputBusyError):
        provider.open_stream(
            phase="full_duplex", sample_rate=16_000, channels=1, dtype="int16", blocksize=480
        )
    ordinary.close(timeout=0.2)
    replacement = provider.open_stream(
        phase="full_duplex", sample_rate=16_000, channels=1, dtype="int16", blocksize=480
    )
    replacement.close(timeout=0.2)
    assert len(processes) == 2


def test_failed_pulse_reap_keeps_exclusive_ownership(monkeypatch):
    from tools.voice_input import (
        PulseCliVoiceInputProvider,
        VoiceInputBusyError,
        VoiceInputReleaseError,
    )

    class _StuckProcess(_FakeProcess):
        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1

        def wait(self, timeout=None):
            self.waited += 1
            raise subprocess.TimeoutExpired("parec", timeout)

    process = _StuckProcess([b""])
    monkeypatch.setattr("tools.voice_input.subprocess.Popen", lambda *_a, **_k: process)
    provider = PulseCliVoiceInputProvider(executable="parec")
    stream = provider.open_stream(
        phase="ordinary", sample_rate=16_000, channels=1, dtype="int16", blocksize=480
    )

    with pytest.raises(VoiceInputReleaseError, match="did not release"):
        stream.close(timeout=0.01)
    with pytest.raises(VoiceInputBusyError):
        provider.open_stream(
            phase="ordinary", sample_rate=16_000, channels=1, dtype="int16", blocksize=480
        )
    assert process.terminated == 1 and process.killed == 1


def test_pulse_cli_rejects_noncanonical_pcm_shape(monkeypatch):
    from tools.voice_input import PulseCliVoiceInputProvider

    provider = PulseCliVoiceInputProvider(executable="parec")
    with pytest.raises(ValueError, match="mono 16 kHz int16"):
        provider.open_stream(
            phase="ordinary", sample_rate=48_000, channels=2, dtype="float32", blocksize=480
        )


def test_portaudio_release_attempts_every_shared_stream_after_one_fails():
    from tools.voice_input import PortAudioVoiceInputProvider, VoiceInputReleaseError

    class _Raw:
        def __init__(self, fail=False):
            self.fail = fail
            self.closed = 0

        def close(self):
            self.closed += 1
            if self.fail:
                raise OSError("first close failed")

    first_raw, second_raw = _Raw(fail=True), _Raw()
    raw_queue = [first_raw, second_raw]
    provider = PortAudioVoiceInputProvider(import_audio=lambda: (
        type("SD", (), {"InputStream": staticmethod(lambda **_kwargs: raw_queue.pop(0))})(),
        object(),
    ))
    provider.open_stream(phase="ordinary", sample_rate=16_000, channels=1, dtype="int16")
    provider.open_stream(phase="full_duplex", sample_rate=16_000, channels=1, dtype="int16")

    with pytest.raises(VoiceInputReleaseError):
        provider.release_active(timeout=0.2)

    assert first_raw.closed == 1
    assert second_raw.closed == 1


def test_exclusive_release_waits_for_blocked_physical_start():
    from tools.voice_input import PortAudioVoiceInputProvider

    start_entered, allow_start, release_started, release_done = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event()
    )

    class _Raw:
        active = False

        def start(self):
            start_entered.set()
            assert allow_start.wait(2)
            self.active = True

        def stop(self):
            self.active = False

        def close(self):
            self.active = False

    provider = PortAudioVoiceInputProvider(
        import_audio=lambda: (
            type("SD", (), {"InputStream": staticmethod(lambda **_kwargs: _Raw())})(),
            object(),
        ),
        requires_exclusive_handoff=True,
    )
    stream = provider.open_stream(
        phase="ordinary", sample_rate=16_000, channels=1, dtype="int16"
    )
    starter = threading.Thread(target=stream.start)
    starter.start()
    assert start_entered.wait(1)

    def release():
        release_started.set()
        provider.release_active(timeout=2)
        release_done.set()

    releaser = threading.Thread(target=release)
    releaser.start()
    assert release_started.wait(1)
    assert release_done.wait(0.05) is False
    allow_start.set()
    starter.join(2)
    releaser.join(2)

    assert release_done.is_set()
    assert stream.active is False
