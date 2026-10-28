"""Process-scoped microphone input providers.

Only physical PCM capture and release live here. VAD, endpointing, streaming
transcription, and TTS policy remain in :mod:`tools.voice_mode`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from array import array
from typing import Any, Callable, Mapping


class VoiceInputError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "open", retryable: bool = True):
        super().__init__(message)
        self.stage = stage
        self.retryable = bool(retryable)


class VoiceInputBusyError(VoiceInputError):
    def __init__(self, message: str):
        super().__init__(message, stage="open", retryable=True)


class VoiceInputEOF(VoiceInputError):
    def __init__(self, message: str = "voice input reached EOF"):
        super().__init__(message, stage="read", retryable=False)


class VoiceInputReleaseError(VoiceInputError):
    def __init__(self, message: str):
        super().__init__(message, stage="close", retryable=False)


class _Pcm16Block:
    """Small numpy-compatible fallback used by the Pulse reader in lean installs."""

    def __init__(self, raw: bytes):
        values = array("h")
        values.frombytes(raw)
        if sys.byteorder != "little":
            values.byteswap()
        self._values = values

    def reshape(self, *_shape):
        return self

    def tolist(self):
        return self._values.tolist()

    def copy(self):
        return _Pcm16Block(self._values.tobytes())

    def tobytes(self):
        raw = array("h", self._values)
        if sys.byteorder != "little":
            raw.byteswap()
        return raw.tobytes()

    def astype(self, *_args, **_kwargs):
        return self

    def __len__(self):
        return len(self._values)


class _ExclusiveProvider:
    requires_exclusive_handoff = True

    def __init__(self):
        self._phase_lock = threading.Lock()
        self._phase_cond = threading.Condition(self._phase_lock)
        self._active_token: object | None = None
        self._active_phase = ""
        self._active_stream: Any | None = None

    def _claim(self, phase: str) -> object:
        phase = str(phase or "ordinary").strip() or "ordinary"
        with self._phase_cond:
            if self._active_token is not None:
                raise VoiceInputBusyError(
                    f"voice input is already owned by the {self._active_phase} phase"
                )
            token = object()
            self._active_token = token
            self._active_phase = phase
            return token

    def _activate(self, token: object, stream: Any):
        with self._phase_cond:
            if self._active_token is not token:
                raise VoiceInputReleaseError("voice input ownership changed while opening")
            self._active_stream = stream
            self._phase_cond.notify_all()
        return stream

    def _release(self, token: object) -> None:
        with self._phase_cond:
            if self._active_token is token:
                self._active_token = None
                self._active_phase = ""
                self._active_stream = None
                self._phase_cond.notify_all()

    def release_active(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._phase_cond:
            token, stream, phase = self._active_token, self._active_stream, self._active_phase
            while token is not None and stream is None and self._active_token is token:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VoiceInputReleaseError(
                        f"voice input {phase or 'capture'} phase is still opening"
                    )
                self._phase_cond.wait(timeout=remaining)
                stream = self._active_stream
            if self._active_token is not token:
                return True
        if token is None:
            return True
        stream.close(timeout=max(0.0, deadline - time.monotonic()))
        with self._phase_lock:
            if self._active_token is token:
                raise VoiceInputReleaseError(
                    f"voice input {phase or 'capture'} phase did not release"
                )
        return True


class _PortAudioStream:
    def __init__(self, provider: PortAudioVoiceInputProvider, token: object, raw: Any):
        self._provider = provider
        self._token = token
        self._raw = raw
        self._started = False
        self._entered = False
        self._closed = False
        self._lifecycle_lock = threading.RLock()

    @property
    def active(self):
        return False if self._closed else bool(getattr(self._raw, "active", self._started))

    def start(self):
        with self._lifecycle_lock:
            if self._started:
                return self
            try:
                start = getattr(self._raw, "start", None)
                if callable(start):
                    start()
                else:
                    enter = getattr(self._raw, "__enter__", None)
                    if callable(enter):
                        enter()
                        self._entered = True
            except Exception as exc:
                try:
                    self.close()
                except VoiceInputError as release_error:
                    raise release_error from exc
                raise VoiceInputError(
                    f"failed to start PortAudio voice input: {exc}", stage="open", retryable=True
                ) from exc
            self._started = True
            return self

    def read(self, frames: int):
        try:
            return self._raw.read(frames)
        except VoiceInputError:
            raise
        except Exception as exc:
            raise VoiceInputError(
                f"voice input read failed: {exc}", stage="read", retryable=True
            ) from exc

    def close(self, timeout: float = 3.0):
        if not self._lifecycle_lock.acquire(timeout=max(0.0, float(timeout))):
            raise VoiceInputReleaseError("voice input start is still in progress")
        try:
            if self._closed:
                return
            errors: list[BaseException] = []
            if self._entered:
                try:
                    self._raw.__exit__(None, None, None)
                except BaseException as exc:
                    errors.append(exc)
            else:
                stop = getattr(self._raw, "stop", None)
                close = getattr(self._raw, "close", None)
                if self._started and callable(stop):
                    try:
                        stop()
                    except BaseException as exc:
                        errors.append(exc)
                if callable(close):
                    try:
                        close()
                    except BaseException as exc:
                        errors.append(exc)
            if errors:
                raise VoiceInputReleaseError(f"voice input close failed: {errors[0]}") from errors[0]
            self._closed = True
            self._provider._release(self._token)
        finally:
            self._lifecycle_lock.release()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class PortAudioVoiceInputProvider(_ExclusiveProvider):
    def __init__(
        self,
        *,
        import_audio: Callable[[], tuple[Any, Any]] | None = None,
        requires_exclusive_handoff: bool = False,
    ):
        super().__init__()
        self._import_audio = import_audio or self._default_import_audio
        self.requires_exclusive_handoff = bool(requires_exclusive_handoff)
        self._shared_streams: dict[object, _PortAudioStream] = {}

    @staticmethod
    def _default_import_audio():
        import numpy as np
        import sounddevice as sd

        return sd, np

    def _portaudio_claim(self, phase: str) -> object:
        return self._claim(phase) if self.requires_exclusive_handoff else object()

    def _portaudio_activate(self, token: object, stream: _PortAudioStream):
        if self.requires_exclusive_handoff:
            return self._activate(token, stream)
        with self._phase_lock:
            self._shared_streams[token] = stream
        return stream

    def _release(self, token: object) -> None:
        if self.requires_exclusive_handoff:
            super()._release(token)
            return
        with self._phase_lock:
            self._shared_streams.pop(token, None)

    def preferred_sample_rate(self) -> int:
        sd, _ = self._import_audio()
        try:
            info = sd.query_devices(None, "input")
            rate = info.get("default_samplerate") if isinstance(info, dict) else getattr(
                info, "default_samplerate", None
            )
            if isinstance(rate, (int, float)) and rate > 0:
                return round(rate)
        except Exception:
            pass
        return 16_000

    def is_available(self) -> bool:
        try:
            self._import_audio()
            return True
        except (ImportError, OSError):
            return False

    def open_stream(
        self,
        *,
        phase: str,
        sample_rate: int,
        channels: int,
        dtype: str,
        blocksize: int = 0,
        callback=None,
        on_error=None,
    ):
        del on_error
        token = self._portaudio_claim(phase)
        try:
            sd, _ = self._import_audio()
            kwargs = {"samplerate": sample_rate, "channels": channels, "dtype": dtype}
            if blocksize:
                kwargs["blocksize"] = blocksize
            if callback is not None:
                kwargs["callback"] = callback
            raw = sd.InputStream(**kwargs)
        except Exception as exc:
            self._release(token)
            if isinstance(exc, VoiceInputError):
                raise
            raise VoiceInputError(
                f"failed to open PortAudio voice input: {exc}", stage="open", retryable=True
            ) from exc
        return self._portaudio_activate(token, _PortAudioStream(self, token, raw))

    def release_active(self, timeout: float = 3.0) -> bool:
        if self.requires_exclusive_handoff:
            return super().release_active(timeout=timeout)
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._phase_lock:
            streams = list(self._shared_streams.values())
        errors: list[BaseException] = []
        for stream in streams:
            try:
                stream.close(timeout=max(0.0, deadline - time.monotonic()))
            except BaseException as exc:
                errors.append(exc)
        with self._phase_lock:
            remaining = bool(self._shared_streams)
        if errors or remaining:
            detail = str(errors[0]) if errors else "one or more streams remain owned"
            raise VoiceInputReleaseError(f"PortAudio voice input streams did not release: {detail}")
        return True


class _PulseCliStream:
    def __init__(self, provider, token, process, *, blocksize, callback, on_error):
        self._provider = provider
        self._token = token
        self._process = process
        self._blocksize = max(1, int(blocksize))
        self._callback = callback
        self._on_error = on_error
        self._buffer = bytearray()
        self._terminal_error: VoiceInputEOF | None = None
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def active(self):
        return not self._closed and self._process.poll() is None

    def _read_bytes(self, count: int) -> bytes:
        if self._terminal_error is not None:
            raise self._terminal_error
        stdout = getattr(self._process, "stdout", None)
        if stdout is None:
            self._terminal_error = VoiceInputEOF("parec stdout reached EOF")
            raise self._terminal_error
        while len(self._buffer) < count:
            try:
                chunk = stdout.read(count - len(self._buffer))
            except Exception as exc:
                raise VoiceInputError(
                    f"parec read failed: {exc}", stage="read", retryable=True
                ) from exc
            if not chunk:
                self._terminal_error = VoiceInputEOF("parec stdout reached EOF")
                raise self._terminal_error
            self._buffer.extend(chunk)
        raw = bytes(self._buffer[:count])
        del self._buffer[:count]
        return raw

    def read(self, frames: int):
        raw = self._read_bytes(max(1, int(frames)) * 2)
        try:
            import numpy as np
        except ImportError:
            data = _Pcm16Block(raw)
        else:
            data = np.frombuffer(raw, dtype="<i2").reshape(-1, 1).copy()
        return data, False

    def start(self):
        if self._callback is None or self._pump is not None:
            return self

        def pump():
            try:
                while not self._stop.is_set():
                    data, overflowed = self.read(self._blocksize)
                    self._callback(data, len(data), None, overflowed)
            except VoiceInputError as exc:
                if not self._stop.is_set() and self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass

        self._pump = threading.Thread(target=pump, name="hermes-pulse-input", daemon=True)
        self._pump.start()
        return self

    def close(self, timeout: float = 3.0):
        with self._close_lock:
            if self._closed:
                return
            self._stop.set()
            deadline = time.monotonic() + max(0.0, float(timeout))
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
                if self._pump is not None and self._pump is not threading.current_thread():
                    self._pump.join(timeout=max(0.0, deadline - time.monotonic()))
                    if self._pump.is_alive():
                        raise VoiceInputReleaseError(
                            f"parec reader did not stop within {float(timeout):.1f}s"
                        )
            except VoiceInputReleaseError:
                raise
            except Exception as exc:
                raise VoiceInputReleaseError(f"parec process did not release: {exc}") from exc
            stdout = getattr(self._process, "stdout", None)
            if callable(getattr(stdout, "close", None)):
                try:
                    stdout.close()
                except Exception:
                    pass
            self._closed = True
            self._provider._release(self._token)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class PulseCliVoiceInputProvider(_ExclusiveProvider):
    requires_exclusive_handoff = True

    def __init__(self, *, pulse_server=None, pulse_source=None, executable="parec"):
        super().__init__()
        self.pulse_server = pulse_server if pulse_server is not None else os.environ.get("PULSE_SERVER")
        self.pulse_source = pulse_source or os.environ.get("PULSE_SOURCE") or "echo-cancel-source"
        self.executable = executable or "parec"

    def preferred_sample_rate(self) -> int:
        return 16_000

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def open_stream(
        self,
        *,
        phase: str,
        sample_rate: int,
        channels: int,
        dtype: str,
        blocksize: int = 480,
        callback=None,
        on_error=None,
    ):
        if int(sample_rate) != 16_000 or int(channels) != 1 or dtype != "int16":
            raise ValueError("pulse_cli requires mono 16 kHz int16 PCM")
        token = self._claim(phase)
        from tools.environments.local import hermes_subprocess_env

        env = hermes_subprocess_env(inherit_credentials=False)
        if self.pulse_server is not None:
            env["PULSE_SERVER"] = self.pulse_server
        env["PULSE_SOURCE"] = self.pulse_source
        argv = [
            self.executable,
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            f"--device={self.pulse_source}",
        ]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as exc:
            self._release(token)
            raise VoiceInputError(
                f"failed to open pulse_cli voice input: {exc}", stage="open", retryable=True
            ) from exc
        return self._activate(
            token,
            _PulseCliStream(
                self,
                token,
                process,
                blocksize=blocksize,
                callback=callback,
                on_error=on_error,
            ),
        )


def _voice_input_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            loaded = load_config()
            config = loaded if isinstance(loaded, dict) else {}
        except Exception:
            config = {}
    root = dict(config) if isinstance(config, Mapping) else {}
    voice = root.get("voice")
    voice = voice if isinstance(voice, Mapping) else {}
    input_config = voice.get("input")
    return dict(input_config) if isinstance(input_config, Mapping) else {}


def _config_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def create_voice_input_provider(config=None, *, import_audio=None):
    cfg = _voice_input_config(config)
    name = str(cfg.get("provider", "portaudio") or "portaudio").strip().lower()
    if name == "portaudio":
        return PortAudioVoiceInputProvider(
            import_audio=import_audio,
            requires_exclusive_handoff=_config_bool(cfg.get("exclusive_handoff", False)),
        )
    if name == "pulse_cli":
        pulse = cfg.get("pulse_cli")
        pulse = pulse if isinstance(pulse, Mapping) else {}
        return PulseCliVoiceInputProvider(
            pulse_server=pulse.get("server") or None,
            pulse_source=pulse.get("source"),
            executable=str(pulse.get("executable", "parec") or "parec"),
        )
    raise ValueError(f"unsupported voice input provider: {name}")


_PROVIDER_CACHE: dict[tuple[Any, ...], Any] = {}
_PROVIDER_CACHE_LOCK = threading.Lock()


def get_voice_input_provider(config=None, *, import_audio=None):
    cfg = _voice_input_config(config)
    pulse = cfg.get("pulse_cli")
    pulse = pulse if isinstance(pulse, Mapping) else {}
    name = str(cfg.get("provider", "portaudio") or "portaudio").strip().lower()
    key = (
        name,
        _config_bool(cfg.get("exclusive_handoff", False)),
        pulse.get("server"),
        pulse.get("source"),
        pulse.get("executable", "parec"),
        id(import_audio) if name == "portaudio" else None,
    )
    with _PROVIDER_CACHE_LOCK:
        provider = _PROVIDER_CACHE.get(key)
        if provider is None:
            provider = create_voice_input_provider(config, import_audio=import_audio)
            _PROVIDER_CACHE[key] = provider
        return provider


def configured_voice_input_provider(config=None) -> str:
    cfg = _voice_input_config(config)
    return str(cfg.get("provider", "portaudio") or "portaudio").strip().lower()


def voice_input_requires_exclusive_handoff(config=None) -> bool:
    return bool(get_voice_input_provider(config).requires_exclusive_handoff)


def release_voice_input(*, timeout: float = 3.0, exclusive_only: bool = False) -> bool:
    """Synchronously release cached physical input streams.

    ``exclusive_only`` keeps the default persistent PortAudio stream intact while
    still providing the RK handoff barrier.
    """
    with _PROVIDER_CACHE_LOCK:
        providers = list({id(p): p for p in _PROVIDER_CACHE.values()}.values())
    failures = []
    for provider in providers:
        if exclusive_only and not provider.requires_exclusive_handoff:
            continue
        try:
            if provider.release_active(timeout=timeout) is False:
                raise VoiceInputReleaseError("provider reported an incomplete release")
        except Exception as exc:
            failures.append((type(provider).__name__, exc))
    if failures:
        details = "; ".join(f"{name}: {exc}" for name, exc in failures)
        raise VoiceInputReleaseError(
            f"one or more voice input providers failed to release: {details}"
        ) from failures[0][1]
    return True


def open_voice_input_stream(*, config=None, import_audio=None, **kwargs):
    return get_voice_input_provider(config, import_audio=import_audio).open_stream(**kwargs)
