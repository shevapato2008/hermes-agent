"""Voice / TTS / wake-word JSON-RPC handlers and their process-global state (one mic, one speaker
per process). Bodies are rebound onto server.py's globals (method_ctx.bind_module), used bare.
"""

from __future__ import annotations

import contextlib
import threading
import uuid

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


# ── Voice state: HERMES_VOICE / HERMES_VOICE_TTS are runtime-only env flags (never config.yaml)
# so a prior session can't auto-start REC.

_voice_sid_lock = threading.Lock()
_voice_event_sid: str = ""
_voice_wake_owner: Optional[Transport] = None
# The transport that started the capture. A shared-owner (canonical) session is not in this process's
# ``_sessions``, so ``write_json`` cannot route its voice events by sid; they go to this caller instead.
_voice_event_transport: Optional[Transport] = None
# The session whose push-to-talk capture is live, and a generation that invalidates its callbacks
# when that session's voice mode goes off (a late transcript must not land after /voice off).
_voice_capture_sid: str = ""
_voice_capture_gen = 0

# One physical microphone per process. The owner remains installed while a
# bounded physical close is running or has failed, so a second reader can never
# slip in between a logical stop and actual device release.
_voice_lease_owner_sid: str = ""
_voice_lease_owner_transport: Optional[Transport] = None
_voice_lease_epoch = 0
_voice_lease_releasing = False
_voice_lease_capture_id: str | None = None
_voice_release_tail_epoch: int | None = None
_voice_pending_idle_scope: tuple[str, int, Optional[Transport], str | None] | None = None
_voice_terminal_error_epochs: set[int] = set()
_voice_release_retry_active = False
_voice_lifecycle_lock = threading.RLock()
_VOICE_RELEASE_RETRY_SECONDS = 1.0


def _caller_transport():
    return current_transport() or _stdio_transport


def _voice_scope_snapshot() -> tuple[str, int, Optional[Transport], str | None]:
    with _voice_sid_lock:
        return (
            _voice_lease_owner_sid,
            _voice_lease_epoch,
            _voice_lease_owner_transport,
            _voice_lease_capture_id,
        )


def _voice_scope_matches(
    session_id: str, lease_epoch: int, transport=None, *, allow_released: bool = False,
) -> bool:
    with _voice_sid_lock:
        if lease_epoch <= 0 or lease_epoch != _voice_lease_epoch:
            return False
        if _voice_lease_releasing and not allow_released:
            return False
        if _voice_lease_owner_transport is None:
            return bool(allow_released)
        return (
            session_id == _voice_lease_owner_sid
            and (transport is None or transport is _voice_lease_owner_transport)
        )


def _voice_cleanup_scope_matches(session_id: str, lease_epoch: int, transport) -> bool:
    """Exact owner check used only by the physical-release reaper."""
    with _voice_sid_lock:
        return (
            session_id == _voice_lease_owner_sid
            and lease_epoch == _voice_lease_epoch
            and transport is _voice_lease_owner_transport
        )


@contextlib.contextmanager
def _voice_input_start_barrier(session_id: str, lease_epoch: int, transport):
    """Atomically fence lease validity with provider open/start."""
    with _voice_lifecycle_lock:
        with _voice_sid_lock:
            allowed = (
                not _voice_lease_releasing
                and session_id == _voice_lease_owner_sid
                and lease_epoch == _voice_lease_epoch
                and transport is _voice_lease_owner_transport
            )
        yield allowed


def _acquire_voice_lease(session_id: str, transport, *, capture_id: str | None = None) -> int | None:
    global _voice_lease_owner_sid, _voice_lease_owner_transport, _voice_lease_epoch
    global _voice_lease_releasing, _voice_lease_capture_id
    global _voice_release_tail_epoch, _voice_pending_idle_scope
    with _voice_sid_lock:
        if _voice_lease_owner_transport is not None:
            if (not _voice_lease_releasing and session_id == _voice_lease_owner_sid
                    and transport is _voice_lease_owner_transport):
                if capture_id and not _voice_lease_capture_id:
                    _voice_lease_capture_id = capture_id
                return _voice_lease_epoch
            if (
                not _voice_lease_releasing
                and _voice_lease_owner_sid == "__local_voice__"
                and transport is _voice_lease_owner_transport
                and session_id
                and session_id != "__local_voice__"
            ):
                _voice_lease_owner_sid = session_id
                if capture_id:
                    _voice_lease_capture_id = capture_id
                return _voice_lease_epoch
            return None
        if _voice_lease_releasing:
            return None
        _voice_lease_epoch += 1
        _voice_lease_owner_sid = session_id
        _voice_lease_owner_transport = transport
        _voice_lease_capture_id = capture_id
        _voice_release_tail_epoch = None
        _voice_pending_idle_scope = None
        _voice_terminal_error_epochs.discard(_voice_lease_epoch)
        return _voice_lease_epoch


def _voice_scope_payload(
    payload: dict | None, session_id: str, lease_epoch: int, capture_id: str | None = None,
) -> dict:
    scoped = dict(payload or {})
    scoped.update(owner_session_id=session_id, lease_epoch=lease_epoch)
    if capture_id:
        scoped["capture_id"] = capture_id
    return scoped


def _voice_result_scope(
    *, session_id: str | None = None, lease_epoch: int | None = None,
    capture_id: str | None = None, **extra,
) -> dict:
    if session_id is None or lease_epoch is None:
        owner, epoch, _transport, current_capture = _voice_scope_snapshot()
        session_id = owner if session_id is None else session_id
        lease_epoch = epoch if lease_epoch is None else lease_epoch
        capture_id = capture_id or current_capture
    return _voice_scope_payload(extra, session_id or "", int(lease_epoch or 0), capture_id)


def _voice_emit(
    event: str,
    payload: dict | None = None,
    *,
    session_id: str | None = None,
    lease_epoch: int | None = None,
    transport=None,
    capture_id: str | None = None,
    require_current: bool = True,
) -> bool:
    """Emit a voice event with its captured lease, never mutable global routing."""
    if session_id is None or lease_epoch is None or transport is None:
        owner, epoch, owner_transport, current_capture = _voice_scope_snapshot()
        session_id = owner if session_id is None else session_id
        lease_epoch = epoch if lease_epoch is None else lease_epoch
        transport = owner_transport if transport is None else transport
        capture_id = capture_id or current_capture
    if require_current and not _voice_scope_matches(
            session_id or "", int(lease_epoch or 0), transport, allow_released=True):
        return False
    sid = session_id or ""
    scoped = _voice_scope_payload(payload, sid, int(lease_epoch or 0), capture_id)
    token = bind_transport(transport) if transport is not None else None
    try:
        return bool(_emit(event, sid, scoped))
    finally:
        if token is not None:
            reset_transport(token)


def _voice_release_timeout() -> float:
    raw = _voice_cfg_dict().get("input")
    raw = raw if isinstance(raw, dict) else {}
    value = raw.get("release_timeout_seconds", 3.0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 3.0


def _release_voice_lease(
    *, session_id: str, lease_epoch: int, transport, stop_loop: bool,
    force_transcribe: bool = False, _retry: bool = False, before_expose=None,
) -> tuple[bool, str]:
    """Invalidate/release one captured owner only after physical input closes."""
    global _voice_lease_owner_sid, _voice_lease_owner_transport, _voice_lease_releasing
    global _voice_lease_capture_id
    with _voice_sid_lock:
        if (_voice_lease_owner_transport is None and lease_epoch == _voice_lease_epoch):
            return True, ""
        if not (session_id == _voice_lease_owner_sid and lease_epoch == _voice_lease_epoch
                and transport is _voice_lease_owner_transport):
            return False, "voice input lease is owned by another session"
        if _voice_lease_releasing and not _retry:
            return False, "voice input release is still in progress"
        _voice_lease_releasing = True
    try:
        with _voice_lifecycle_lock:
            if stop_loop:
                from hermes_cli.voice import stop_continuous
                stop_continuous(force_transcribe=force_transcribe)
            from tools.voice_input import release_voice_input
            release_voice_input(timeout=_voice_release_timeout(), exclusive_only=True)
            if before_expose is not None:
                before_expose()
    except Exception as exc:
        # Keep the cleanup fence and captured transport installed: even the
        # same client must not reopen the mic while the failed close is reaped.
        _schedule_voice_release_retry(session_id, lease_epoch, transport)
        return False, str(exc)
    _flush_pending_voice_idle(session_id, lease_epoch, transport)
    with _voice_sid_lock:
        if not (session_id == _voice_lease_owner_sid and lease_epoch == _voice_lease_epoch
                and transport is _voice_lease_owner_transport):
            _voice_lease_releasing = False
            return False, "voice input lease changed during release"
        _voice_lease_owner_sid = ""
        _voice_lease_owner_transport = None
        _voice_lease_capture_id = None
        _voice_lease_releasing = False
    return True, ""


def _mark_voice_release_tail(lease_epoch: int) -> None:
    global _voice_release_tail_epoch
    with _voice_sid_lock:
        if lease_epoch == _voice_lease_epoch:
            _voice_release_tail_epoch = lease_epoch


def _flush_pending_voice_idle(session_id: str, lease_epoch: int, transport) -> None:
    """Publish an early forced-stop idle only after physical release completed."""
    global _voice_pending_idle_scope, _voice_release_tail_epoch
    with _voice_sid_lock:
        scope = _voice_pending_idle_scope
        if (
            scope is None
            or scope[0] != session_id
            or scope[1] != lease_epoch
            or scope[2] is not transport
        ):
            return
        _voice_pending_idle_scope = None
        if _voice_release_tail_epoch == lease_epoch:
            _voice_release_tail_epoch = None
    _voice_emit(
        "voice.status",
        {"state": "idle", "outcome": "complete", "released": True,
         "release_complete": True},
        session_id=session_id, lease_epoch=lease_epoch, transport=transport,
        capture_id=scope[3])


def _input_error_payload(error, *, released: bool) -> dict:
    stage = str(getattr(error, "stage", "provider") or "provider")
    retryable = bool(getattr(error, "retryable", True))
    return {
        "message": str(error), "stage": stage, "retryable": retryable,
        "fatal": not retryable, "outcome": "error", "released": released,
        "release_complete": released,
    }


def _schedule_voice_release_retry(session_id: str, lease_epoch: int, transport) -> None:
    global _voice_release_retry_active
    with _voice_sid_lock:
        if _voice_release_retry_active:
            return
        _voice_release_retry_active = True

    def retry():
        global _voice_release_retry_active
        try:
            while _voice_cleanup_scope_matches(session_id, lease_epoch, transport):
                time.sleep(_VOICE_RELEASE_RETRY_SECONDS)
                released, _error = _release_voice_lease(
                    session_id=session_id, lease_epoch=lease_epoch, transport=transport,
                    stop_loop=True, _retry=True)
                if released:
                    _resume_voice_wake()
                    return
        finally:
            with _voice_sid_lock:
                _voice_release_retry_active = False
    threading.Thread(target=retry, name="voice-release-retry", daemon=True).start()


def _release_voice_for_transport(transport) -> bool:
    session_id, lease_epoch, owner, _capture_id = _voice_scope_snapshot()
    if owner is not transport:
        return False
    released, _error = _release_voice_lease(
        session_id=session_id, lease_epoch=lease_epoch, transport=transport, stop_loop=True)
    if not released:
        _schedule_voice_release_retry(session_id, lease_epoch, transport)
    return released


def _resume_voice_wake() -> None:
    global _voice_wake_owner
    with _voice_sid_lock:
        owner, _voice_wake_owner = _voice_wake_owner, None
    if owner is not None:
        _wake_resume_if_owner(owner)


def _voice_mode_enabled() -> bool:
    return os.environ.get("HERMES_VOICE", "").strip() == "1"


def _session_voice_enabled(session_id) -> bool:
    """Push-to-talk gate: a shared-owner local session's own gateway ``/voice`` mode (what Ink's
    ``/voice on`` sets through slash.exec, T9); the sidecar's env flag for its own sessions."""
    try:
        from gateway.session_config import local_voice_enabled
        held = local_voice_enabled(session_id)
    except ImportError:
        held = None
    return _voice_mode_enabled() if held is None else held


def _set_session_voice_off(session_id) -> bool:
    """Turn a shared-owner session's own voice mode off (spoken stop, no-speech limit); False when
    no owner in this process holds it (the sidecar's env flag is the state)."""
    try:
        from gateway.session_config import set_local_voice_mode
        return set_local_voice_mode(session_id, "off") is True
    except ImportError:
        return False


def _stop_session_voice_capture(session_id) -> bool:
    """Release *session_id*'s capture without touching another owner's input."""
    global _voice_capture_gen
    owner, epoch, transport, capture_id = _voice_scope_snapshot()
    if not owner:
        return True
    if not session_id or session_id != owner:
        return False
    with _voice_sid_lock:
        _voice_capture_gen += 1
    released, error = _release_voice_lease(
        session_id=owner, lease_epoch=epoch, transport=transport, stop_loop=True)
    if not released:
        from tools.voice_input import VoiceInputReleaseError
        _voice_terminal_error_epochs.add(epoch)
        _voice_emit(
            "voice.input_error",
            _input_error_payload(VoiceInputReleaseError(error), released=False),
            session_id=owner, lease_epoch=epoch, transport=transport,
            capture_id=capture_id)
        return False
    _resume_voice_wake()
    return True


def _capture_live() -> bool:
    try:
        from hermes_cli.voice import is_continuous_active
    except ImportError:  # an older voice wrapper without the probe
        return False
    return bool(is_continuous_active())


def _voice_capture_callback(fn, *, session_id=None, lease_epoch=None, transport=None):
    """*fn* while the capture it was registered for is still current; a no-op once invalidated."""
    with _voice_sid_lock:
        gen = _voice_capture_gen
    if session_id is None or lease_epoch is None or transport is None:
        owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
        session_id = owner if session_id is None else session_id
        lease_epoch = epoch if lease_epoch is None else lease_epoch
        transport = owner_transport if transport is None else transport

    def call(*args):
        with _voice_sid_lock:
            current = gen == _voice_capture_gen
            allow_release_tail = _voice_release_tail_epoch == lease_epoch
        if current and _voice_scope_matches(
                session_id or "", int(lease_epoch or 0), transport,
                allow_released=allow_release_tail):
            fn(*args)
    return call


def _voice_tts_enabled() -> bool:
    return os.environ.get("HERMES_VOICE_TTS", "").strip() == "1"


def _end_voice_chat(*, stop_loop: bool, stop_tts: bool) -> None:
    """Flip voice + TTS off; optionally halt the continuous loop / cut live TTS (best-effort)."""
    os.environ["HERMES_VOICE"] = os.environ["HERMES_VOICE_TTS"] = "0"
    _cancel_output_drain_monitor()
    if stop_loop:
        with contextlib.suppress(Exception):
            from hermes_cli.voice import stop_continuous
            stop_continuous()
    if stop_tts:
        with contextlib.suppress(Exception):
            _tts_stream_stop(user_barge=False)


def _end_session_voice_chat(session_id, *, stop_loop: bool, stop_tts: bool) -> bool:
    """A stop phrase (spoken, typed or full-duplex) ends *session_id*'s voice chat: a shared-owner
    session through its own mode (which ends only its capture); else the sidecar's env flags."""
    if stop_loop:
        _owner, _epoch, owner_transport, _capture_id = _voice_scope_snapshot()
        if owner_transport is None:
            with _voice_sid_lock:
                legacy_transport = _voice_event_transport
            if _acquire_voice_lease(
                    str(session_id or ""), legacy_transport or _caller_transport()) is None:
                return False
    if stop_loop and not _stop_session_voice_capture(session_id):
        return False
    if not _set_session_voice_off(session_id):
        _end_voice_chat(stop_loop=False, stop_tts=stop_tts)
    elif stop_tts:
        with contextlib.suppress(Exception):
            _tts_stream_stop(user_barge=False)
    return True


def _tts_lease_async(lease: str, active: bool) -> None:
    """Acquire/release a TTS lease off the RPC thread (acquiring warms a local engine; must not
    block the toggle's reply). Best-effort."""
    def _run():
        try:
            from tools.tts_tool_lifecycle import acquire_tts_lease, release_tts_lease
            (acquire_tts_lease if active else release_tts_lease)(lease)
        except Exception as e:
            logger.debug("voice: tts lease %s active=%s failed: %s", lease, active, e)
    threading.Thread(target=_run, name=f"tts-lease-{lease}", daemon=True).start()


def _running_sessions() -> list:
    with _sessions_lock:
        return [s for s in _sessions.values() if s.get("running")]


def _any_session_running() -> bool:
    """Voice busy-probe: silent captures during a long turn don't count toward the no-speech limit."""
    try:
        return bool(_running_sessions())
    except Exception:
        return False


def _voice_output_scope_allowed(session_id: str | None, transport) -> bool:
    """Allow turn audio only for the exact lease owner, claiming an idle voice lease if needed."""
    _owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
    if owner_transport is not None:
        if not session_id or transport is None:
            return False
        acquired = _acquire_voice_lease(str(session_id), transport)
        return acquired == epoch and _voice_scope_matches(
            str(session_id), epoch, transport)
    if not _voice_mode_enabled():
        return True  # native TTS-only behavior without a voice/microphone lease
    return _full_duplex_scope(session_id, transport) is not None


@contextlib.contextmanager
def _voice_output_start_barrier(session_id: str | None, transport):
    """Fence output startup against release cleanup and successor acquisition."""
    with _voice_lifecycle_lock:
        yield _voice_output_scope_allowed(session_id, transport)


# ── Streaming TTS: one pipeline per process (one speaker); a new turn's pipeline barges in on
# the previous. Token deltas feed a sentence-buffering consumer (stream_tts_to_speaker).

_tts_stream_lock = threading.Lock()
_tts_stream_state: Optional[dict] = None


def _tts_stream_begin(session_id: str | None = None, transport=None) -> Optional[queue.Queue]:
    """Start a per-turn streaming TTS consumer; None when TTS can't stream."""
    if not _voice_tts_enabled():
        return None
    try:
        from tools.tts_tool import check_tts_requirements
        from tools.tts_tool_speaker import stream_tts_to_speaker
        if not check_tts_requirements():
            return None
    except Exception:
        return None
    global _tts_stream_state
    with _voice_output_start_barrier(session_id, transport) as allowed:
        if not allowed:
            return None
        _tts_stream_stop()
        text_queue: queue.Queue = queue.Queue()
        stop, done = threading.Event(), threading.Event()
        threading.Thread(
            target=stream_tts_to_speaker, args=(text_queue, stop, done), daemon=True
        ).start()
        with _tts_stream_lock:
            _tts_stream_state = {"stop": stop, "done": done}
        _arm_barge_listener_if_enabled(session_id, transport)
        return text_queue


def _tts_stream_stop(user_barge: bool = True) -> None:
    """Cut in-flight streaming TTS. *user_barge* latches the interruption for the next turn's
    model note; ``False`` for mode changes (/voice off)."""
    global _tts_stream_state
    with _tts_stream_lock:
        state, _tts_stream_state = _tts_stream_state, None
    if state is None:
        return
    if user_barge and not state["done"].is_set():
        import traceback
        from tools.tts_streaming import mark_speech_interrupted
        logger.debug("TTS CUT: _tts_stream_stop(user_barge=True) — new turn or "
                     "interrupt cutting in-flight TTS\n%s", "".join(traceback.format_stack()))
        mark_speech_interrupted()
    state["stop"].set()
    with contextlib.suppress(Exception):
        from tools.voice_mode import stop_playback
        stop_playback()


# ── Full-duplex agent-turn listener: arms at utterance-submit, spans generation AND playback
# (per-playback monitors were deaf during generation and mis-calibrated against speaker bleed),
# disarms when no session runs, no TTS is pending, and no audio flows. _fd_speak_pipelines holds
# (stop, done) pairs of fallback whole-reply speak paths: the listener cuts their private stop
# events too, and keeps listening while any is still speaking.

_fd_listener_lock = threading.Lock()
_fd_listener_active = False
_fd_listener_epoch = 0
_fd_speak_pipelines: set[tuple[threading.Event, threading.Event]] = set()
_output_drain_lock = threading.Lock()
_output_drain_stop: threading.Event | None = None


def _full_duplex_scope(session_id: str | None = None, transport=None):
    """Resolve or acquire the one logical microphone lease for a listener."""
    owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
    if owner_transport is not None:
        if session_id and str(session_id) != owner:
            return None
        if transport is not None and transport is not owner_transport:
            return None
        return owner, epoch, owner_transport

    sid = str(session_id or "")
    session_transport = None
    if not sid:
        with _sessions_lock:
            running = [(candidate, record) for candidate, record in _sessions.items()
                       if record.get("running")]
        if len(running) == 1:
            sid, record = running[0]
            session_transport = record.get("transport")
    if not sid:
        with _voice_sid_lock:
            sid = _voice_event_sid
            session_transport = _voice_event_transport
    # Local voice/TTS can predate a persisted session. It still needs a stable
    # owner key so every callback is epoch-fenced instead of running unleased.
    sid = str(sid or "__local_voice__")
    if transport is None:
        if session_transport is None:
            with _sessions_lock:
                record = _sessions.get(sid)
                session_transport = record.get("transport") if record else None
        transport = session_transport or _caller_transport()
    epoch = _acquire_voice_lease(sid, transport)
    if epoch is None:
        return None
    return sid, epoch, transport


def _arm_full_duplex_listener(session_id: str | None = None, transport=None) -> bool:
    """Arm one full-duplex listener with an immutable session/epoch/transport scope."""
    scope = _full_duplex_scope(session_id, transport)
    if scope is None:
        return False
    _cancel_output_drain_monitor()
    sid, epoch, owner_transport = scope
    global _fd_listener_active, _fd_listener_epoch
    with _fd_listener_lock:
        if _fd_listener_active:
            if _fd_listener_epoch == epoch:
                return True
            # A physically released, stale listener may still be unwinding its
            # transcription. Its finally must not clear this successor slot.
            if _voice_scope_matches(sid, epoch, owner_transport):
                _fd_listener_epoch = epoch
            else:
                return False
        else:
            _fd_listener_active = True
            _fd_listener_epoch = epoch
    threading.Thread(
        target=_full_duplex_listener, args=(sid, epoch, owner_transport),
        daemon=True, name="voice-full-duplex").start()
    return True


def _arm_barge_listener_if_enabled(session_id: str | None = None, transport=None) -> None:
    """Arm the listener when voice mode is on and ``voice.barge_in`` isn't disabled."""
    if _voice_mode_enabled() and _voice_cfg_dict().get("barge_in", True):
        _arm_full_duplex_listener(session_id, transport)


def _fd_tts_pending() -> bool:
    """True while any TTS (streaming pipeline or fallback speak) is unfinished."""
    with _tts_stream_lock:
        state = _tts_stream_state
    with _fd_listener_lock:
        pending = ([state["done"]] if state is not None else []) + [done for _stop, done in _fd_speak_pipelines]
    return any(not done.is_set() for done in pending)


def _cancel_output_drain_monitor() -> None:
    """Cancel the microphone-free monitor for an older turn, if any."""
    global _output_drain_stop
    with _output_drain_lock:
        stop, _output_drain_stop = _output_drain_stop, None
    if stop is not None:
        stop.set()


def _arm_output_drain_monitor(session_id: str, transport=None) -> bool:
    """Emit one scoped output-drained event without opening the microphone."""
    global _output_drain_stop
    scope = _full_duplex_scope(session_id, transport)
    if scope is None or not _voice_mode_enabled():
        return False
    sid, epoch, owner_transport = scope
    stop = threading.Event()
    with _output_drain_lock:
        previous, _output_drain_stop = _output_drain_stop, stop
    if previous is not None:
        previous.set()
    threading.Thread(
        target=_output_drain_monitor,
        args=(sid, epoch, owner_transport, stop),
        daemon=True,
        name="voice-output-drain",
    ).start()
    return True


def _output_drain_monitor(
    session_id: str, lease_epoch: int, transport, stop: threading.Event,
) -> None:
    """Wait until this turn and every backend audio pipeline are idle."""
    global _output_drain_stop
    try:
        from tools.voice_mode import is_audio_output_active

        while not stop.is_set():
            if not _voice_mode_enabled() or not _voice_scope_matches(
                    session_id, lease_epoch, transport):
                return
            with _sessions_lock:
                record = _sessions.get(session_id)
                running = bool(record and record.get("running"))
            if running or _fd_tts_pending() or is_audio_output_active():
                stop.wait(0.05)
                continue
            with _output_drain_lock:
                if _output_drain_stop is not stop or stop.is_set():
                    return
                _output_drain_stop = None
            _voice_emit(
                "voice.output_drained",
                session_id=session_id,
                lease_epoch=lease_epoch,
                transport=transport,
            )
            return
    except Exception as exc:
        logger.debug("output-drain monitor failed: %s", exc)
    finally:
        with _output_drain_lock:
            if _output_drain_stop is stop:
                _output_drain_stop = None


def _full_duplex_listener(session_id: str, lease_epoch: int, transport) -> None:
    """Mic live from utterance-submit to turn-complete; a trip transcribes -> ``voice.transcript``."""
    global _fd_listener_active, _fd_listener_epoch
    input_failed = threading.Event()

    def _on_input_error(error) -> None:
        if input_failed.is_set() or not _voice_scope_matches(
                session_id, lease_epoch, transport):
            return
        input_failed.set()
        from tools.voice_input import VoiceInputError, VoiceInputReleaseError
        if not isinstance(error, VoiceInputError):
            error = VoiceInputError(str(error), stage="read", retryable=True)
        _voice_terminal_error_epochs.add(lease_epoch)
        released, release_error = _release_voice_lease(
            session_id=session_id, lease_epoch=lease_epoch, transport=transport,
            stop_loop=False)
        _voice_emit(
            "voice.input_error", _input_error_payload(error, released=released),
            session_id=session_id, lease_epoch=lease_epoch, transport=transport)
        if not released and getattr(error, "stage", "") != "close":
            _voice_emit(
                "voice.input_error",
                _input_error_payload(VoiceInputReleaseError(release_error), released=False),
                session_id=session_id, lease_epoch=lease_epoch, transport=transport)
        if released:
            _resume_voice_wake()

    try:
        from tools.voice_mode import (full_duplex_listen, is_audio_output_active,
                                      transcribe_recording)

        def _should_stop() -> bool:
            if not _voice_scope_matches(session_id, lease_epoch, transport):
                return True
            with _sessions_lock:
                record = _sessions.get(session_id)
                session_running = bool(record and record.get("running"))
            return not _voice_mode_enabled() or not (
                session_running or _fd_tts_pending() or is_audio_output_active())
        tripped = threading.Event()

        def _on_trigger(phase: str) -> None:
            if _fd_trip(phase, session_id, lease_epoch, transport):
                tripped.set()
        mult, grace_ms = _fd_barge_params(_voice_cfg_dict())
        wav_path = full_duplex_listen(_should_stop, is_playing=is_audio_output_active,
                                      on_trigger=_on_trigger, multiplier=mult or None,
                                      grace_ms=grace_ms, on_input_error=_on_input_error,
                                      start_barrier=lambda: _voice_input_start_barrier(
                                          session_id, lease_epoch, transport))
        with _sessions_lock:
            record = _sessions.get(session_id)
            session_running = bool(record and record.get("running"))
        if (
            not wav_path
            and not tripped.is_set()
            and _voice_scope_matches(session_id, lease_epoch, transport)
            and not session_running
            and not _fd_tts_pending()
            and not is_audio_output_active()
        ):
            _voice_emit(
                "voice.output_drained",
                session_id=session_id,
                lease_epoch=lease_epoch,
                transport=transport,
            )
        if not (wav_path and tripped.is_set()):
            return
        try:
            if not _voice_scope_matches(session_id, lease_epoch, transport):
                return
            try:
                result = transcribe_recording(wav_path)
            except Exception as exc:
                from tools.voice_input import VoiceInputError
                _on_input_error(VoiceInputError(
                    f"voice transcription failed: {exc}", stage="stt", retryable=True))
                return
            if result.get("success") and (result.get("transcript") or "").strip():
                _deliver_fd_transcript(
                    result["transcript"].strip(), session_id, lease_epoch, transport)
            elif result.get("success") and _voice_scope_matches(
                    session_id, lease_epoch, transport):
                _voice_emit(
                    "voice.transcript",
                    {"text": "", "retryable_empty": True, "outcome": "empty"},
                    session_id=session_id,
                    lease_epoch=lease_epoch,
                    transport=transport,
                )
            elif not result.get("success"):
                from tools.voice_input import VoiceInputError
                _on_input_error(VoiceInputError(
                    str(result.get("error") or "voice transcription failed"),
                    stage="stt", retryable=True))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(wav_path)
    except Exception as e:
        logger.debug("full-duplex listener failed: %s", e)
        _on_input_error(e)
    finally:
        with _fd_listener_lock:
            if _fd_listener_epoch == lease_epoch:
                _fd_listener_active = False
                _fd_listener_epoch = 0


def _fd_barge_params(cfg: dict) -> tuple[float, int]:
    """``(threshold multiplier, grace ms)`` from the voice config; malformed -> defaults."""
    def num(conv, key, default):
        try:
            return conv(cfg.get(key, default))
        except (TypeError, ValueError):
            return conv(default)
    mult = num(lambda v: float(v or 0), "barge_in_threshold_multiplier", 0)
    return mult, max(0, num(lambda v: int(float(v) * 1000), "barge_in_grace_seconds", 0.5))


def _fd_trip(
    phase: str, session_id: str | None = None, lease_epoch: int | None = None,
    transport=None,
) -> bool:
    """Listener tripped: latch the interruption, cut TTS FIRST (so a stale reply can never
    speak), and during generation interrupt the owning session through the same
    ``agent.interrupt()`` seam used by ``session.interrupt``."""
    if session_id is None or lease_epoch is None or transport is None:
        owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
        session_id = owner if session_id is None else session_id
        lease_epoch = epoch if lease_epoch is None else lease_epoch
        transport = owner_transport if transport is None else transport
    if not _voice_scope_matches(session_id or "", int(lease_epoch or 0), transport):
        return False
    from tools.tts_streaming import mark_speech_interrupted
    from tools.voice_mode import stop_playback
    mark_speech_interrupted()
    if phase == "playback":
        logger.debug("TTS CUT: full-duplex listener tripped during playback")
    else:
        logger.debug("full-duplex listener tripped during generation — "
                     "interrupting running turn(s)")
    # Cut streaming TTS, every fallback speak pipeline, and the file player.
    _tts_stream_stop(user_barge=True)
    with _fd_listener_lock:
        for _stop, _done in _fd_speak_pipelines:
            _stop.set()
    stop_playback()
    if phase != "playback":
        try:
            with _sessions_lock:
                session = _sessions.get(session_id)
            agent = session.get("agent") if session and session.get("running") else None
            if agent is not None and hasattr(agent, "interrupt"):
                with contextlib.suppress(Exception):
                    agent.interrupt()
        except Exception as e:
            logger.debug("voice interjection interrupt failed: %s", e)
    _voice_emit(
        "voice.interrupted", session_id=session_id, lease_epoch=lease_epoch,
        transport=transport)
    return True


def _deliver_fd_transcript(
    text: str, session_id: str | None = None, lease_epoch: int | None = None,
    transport=None,
) -> bool:
    """Emit the captured interjection; a bare stop phrase also ends the voice chat. The stop
    check must never break delivery (stubbed voice_mode in tests, partial installs)."""
    try:
        from tools.voice_mode_transcript import is_voice_stop_phrase
        is_stop = is_voice_stop_phrase(text)
    except Exception:
        is_stop = False
    if session_id is None or lease_epoch is None or transport is None:
        owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
        session_id = owner if session_id is None else session_id
        lease_epoch = epoch if lease_epoch is None else lease_epoch
        transport = owner_transport if transport is None else transport
    if not _voice_scope_matches(session_id or "", int(lease_epoch or 0), transport):
        return False
    if is_stop:  # release is truthful and complete before the stop acknowledgement
        if not _end_session_voice_chat(session_id, stop_loop=True, stop_tts=False):
            return False
    return _voice_emit(
        "voice.transcript", {"stop_phrase": True, "text": text} if is_stop else {"text": text},
        session_id=session_id, lease_epoch=lease_epoch, transport=transport)


def _speak_text_with_barge(
    text: str, session_id: str | None = None, transport=None,
) -> None:
    """speak_text registered in ``_fd_speak_pipelines`` so the listener can cut it / waits for it."""
    from hermes_cli.voice import speak_text
    with _voice_output_start_barrier(session_id, transport) as allowed:
        if not allowed:
            return
        stop, done = threading.Event(), threading.Event()
        with _fd_listener_lock:
            _fd_speak_pipelines.add((stop, done))

        def _speak():
            try:
                speak_text(text, stop)
            except TypeError:  # older wrapper without the stop_event parameter
                speak_text(text)
            finally:
                done.set()
                with _fd_listener_lock:
                    _fd_speak_pipelines.discard((stop, done))
        threading.Thread(target=_speak, daemon=True).start()
    _arm_barge_listener_if_enabled(session_id, transport)


def _voice_cfg_dict() -> dict:
    """Shape-safe ``voice:`` block (no deep-merged defaults: any YAML shape possible; bad → {}).

    ``_load_cfg()`` does not deep-merge DEFAULT_CONFIG, so both the root AND ``voice`` may be any YAML
    scalar / list / None. A hand-edit like ``voice: true`` or a malformed top-level config that parses to a
    scalar would otherwise break ``.get("…")`` and take every ``voice.*`` branch down with it (Copilot
    round-3..7 review on 19835). Coerce through ``isinstance`` at every level so malformed config falls back
    to an empty dict instead of crashing /voice. See #19835.
    """
    cfg = _load_cfg()
    voice_cfg = cfg.get("voice") if isinstance(cfg, dict) else None
    return voice_cfg if isinstance(voice_cfg, dict) else {}


def _voice_cfg_number(value, default):
    """Numeric config value, else *default*; bool excluded (``silence_threshold: true`` ≠ 1)."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _voice_status_payload(**extra) -> dict:
    """``{enabled, record_key, tts, **extra}``: record_key (default ``ctrl+b``) rides every voice.toggle
    branch so a tts toggle never resets a custom binding."""
    record_key = _voice_cfg_dict().get("record_key")
    record_key = record_key if isinstance(record_key, str) and record_key else "ctrl+b"
    return _voice_result_scope(
        enabled=_voice_mode_enabled(), record_key=record_key, tts=_voice_tts_enabled(), **extra)


# ── Wake word ("Hey Hermes"): process-global detector (one mic). The first eligible transport
# to call wake.start owns it until stop, disconnect, or stream failure; on detection we emit
# wake.detected and the client opens a session + its own capture. The detector yields the mic
# to voice.record (pause/resume) and to the desktop's browser mic (wake.pause/resume RPCs).
_wake_lock = threading.Lock()
_wake_owner_transport: Optional[Transport] = None
_wake_owner_surface = ""


def _wake_owner_snapshot():
    with _wake_lock:
        return _wake_owner_transport, _wake_owner_surface


def _release_wake_for_transport(transport: Transport) -> bool:
    """Release the wake lease iff ``transport`` is the current gateway owner."""
    global _wake_owner_transport, _wake_owner_surface
    with _wake_lock:
        if _wake_owner_transport is not transport:
            return False
        try:
            from tools.wake_word import stop_listening
            stopped = bool(stop_listening(owner=transport))
        except Exception as e:
            logger.debug("wake stop failed: %s", e)
            return False
        if not stopped:
            return False
        _wake_owner_transport, _wake_owner_surface = None, ""
        return True


def _release_gateway_wake_owner() -> bool:
    owner, _surface = _wake_owner_snapshot()
    return owner is not None and _release_wake_for_transport(owner)


_wake_resume_retry_lock = threading.Lock()
_wake_resume_retry_active = False


def _wake_resume_if_owner(owner: Transport, *, retry_seconds: float = 15.0,
                          retry_interval: float = 1.0) -> bool:
    """Resume the wake detector for ``owner``, self-healing a busy microphone: reopening right after
    a voice turn can fail while the device is still being released (browser WebRTC tracks release
    async), so an exception retries in a background thread until it sticks, the lease changes hands,
    or ``retry_seconds`` elapses. ``False`` (lease gone / other owner) is final — never retried."""
    from tools.wake_word import resume_listening
    try:
        return resume_listening(owner=owner)
    except Exception as e:
        logger.debug("wake resume failed (will retry): %s", e)
    global _wake_resume_retry_active
    with _wake_resume_retry_lock:
        if _wake_resume_retry_active:
            return False
        _wake_resume_retry_active = True

    def _retry() -> None:
        global _wake_resume_retry_active
        deadline = time.monotonic() + retry_seconds
        try:
            while time.monotonic() < deadline:
                time.sleep(retry_interval)
                with contextlib.suppress(Exception):
                    if resume_listening(owner=owner):
                        logger.info("wake: detector resumed after retry")
                    return  # False — detector gone or lease moved: stop, don't fight it.
            logger.warning("wake: could not resume detector after voice turn "
                           "(microphone still busy?) — toggle the wake word to re-arm")
        finally:
            with _wake_resume_retry_lock:
                _wake_resume_retry_active = False
    threading.Thread(target=_retry, daemon=True, name="wake-resume-retry").start()
    return False


def _persist_wake_enabled(enabled: bool) -> bool:
    """Write ``wake_word.enabled``; only for explicit gestures (ear toggle, /wake on|off)."""
    try:
        from cli import save_config_value
        return bool(save_config_value("wake_word.enabled", enabled))
    except Exception as e:
        logger.warning("wake: failed to persist wake_word.enabled=%s: %s", enabled, e)
        return False


def _owner_result(rid, field: str, ok, **extra) -> dict:
    """``{field: ok, reason: None | "not_owner", **extra}`` for the owner-gated wake RPCs."""
    return _ok(rid, {field: ok, "reason": None if ok else "not_owner", **extra})


def _frame_fields(frame: dict) -> dict:
    return {"sample_rate": frame.get("sample_rate", 16000), "frame_length": frame.get("frame_length", 1280)}


def _wake_probe(cfg: dict, params: dict, surface: str) -> tuple[str, dict]:
    """``(capture_mode, requirements)``; capture stamped so the probe matches what would arm.
    Desktop (gui) prefers client capture (Mac mic → wake.feed PCM); CLI/TUI stay local."""
    from tools.wake_word import check_wake_word_requirements, resolve_capture_mode
    prefer_client = surface in ("gui", "desktop") or bool(params.get("client_capture"))
    capture_mode = resolve_capture_mode(cfg, prefer_client=prefer_client)
    return capture_mode, check_wake_word_requirements({**cfg, "capture": capture_mode})


def _wake_detect_handler(transport, sid: str, phrase: str, new_session: bool):
    """On-detect callback: pause, verify ownership, emit ``wake.detected`` on the owner's transport."""
    def _on_detect() -> None:
        from tools.wake_word import get_last_match, owns_listener, pause_listening
        if not pause_listening(owner=transport) or not owns_listener(transport):
            return
        if _transport_is_dead(transport):
            _release_wake_for_transport(transport)
            return
        # Multi-phrase engines report WHICH phrase/profile fired; single-phrase engines fall back.
        matched_phrase, matched_profile = get_last_match() or (phrase, "")
        logger.info("wake.detected: emitting to sid=%r (transport=%s, profile=%r)",
                    sid, type(transport).__name__, matched_profile)
        token = bind_transport(transport)
        try:
            _emit("wake.detected", sid, {
                "phrase": matched_phrase or phrase, "profile": matched_profile or None,
                "start_new_session": new_session})
        finally:
            reset_transport(token)
    return _on_detect


@method("gateway.capabilities")
def _(rid, params: dict) -> dict:
    """What THIS BUILD enforces (a client withholds unless advertised), sourced from the enforcing
    module, never config: a believed-but-absent capability is worse."""
    from hermes_cli.active_sessions import PER_SESSION_EXCLUSIVE_SUBMIT
    return _ok(rid, {"per_session_exclusive_submit": bool(PER_SESSION_EXCLUSIVE_SUBMIT)})


@method("client.capabilities")
def _(rid, params: dict) -> dict:
    """What the calling client handles. ``server_requests: true`` marks this connection as one that answers
    server→client requests; a WebSocket client that never sends it gets every such request failed fast
    instead of stalling the agent for the deadline (#112548)."""
    from hermes_cli.process_identity import local_desktop_client
    from tui_gateway import server_requests
    from tui_gateway.contracts import registry as contracts
    transport = _caller_transport()
    # Each call updates what it names: the Desktop sends ``desktop_local`` on its own, so a backend older
    # than the field refuses only that call and keeps the ``server_requests`` it already accepted.
    if "server_requests" in params:
        server_requests.advertise(transport, bool(params.get("server_requests")))
    if "desktop_local" in params and hasattr(transport, "auth_identity"):
        transport.desktop_local = local_desktop_client(transport.auth_identity, params.get("desktop_local"))
        if transport.desktop_local:
            from tui_gateway import server
            server.begin_local_desktop_boot()
    return _ok(rid, {"server_requests": sorted(contracts.SERVER_REQUESTS), "declines_not_shown": True})


@method("ping")
def _(rid, params: dict) -> dict:
    """Cheapest liveness probe, answered on the WS reader thread (works while every agent is mid-turn)
    so the desktop can tell a half-open socket after sleep/wake."""
    return _ok(rid, {"pong": True})


@method("wake.start")
def _(rid, params: dict) -> dict:
    """Arm the wake-word listener for the calling surface ("tui" | "gui"); ``{started: False,
    reason}`` when disabled/owned/not ready. ``persist: true`` (explicit gesture) also flips
    ``wake_word.enabled`` on; auto-arm callers omit it."""
    global _wake_owner_transport, _wake_owner_surface
    surface = str(params.get("surface") or "auto").strip().lower()
    transport = _caller_transport()

    def refused(reason, **extra):
        return _ok(rid, {"started": False, "reason": reason, **extra})
    try:
        from tools.wake_word import (
            WakeWordInUse, detector_frame_info, load_wake_word_config, owns_listener,
            start_listening, wake_phrase, wake_surface_enabled)
    except Exception as e:
        return _err(rid, 5026, f"wake module unavailable: {e}")
    cfg = load_wake_word_config()
    capture_mode, reqs = _wake_probe(cfg, params, surface)
    # Requirements first: a gesture on an un-armable setup must refuse WITHOUT flipping
    # wake_word.enabled — else config says on while nothing can arm.
    if not reqs["available"]:
        logger.warning("wake.start(%s): not available — %s", surface, reqs.get("hint"))
        return refused("unavailable", hint=reqs.get("hint") or "", capture=capture_mode)
    enabled_persisted = bool(params.get("persist")) and not cfg.get("enabled") and _persist_wake_enabled(True)
    if enabled_persisted:
        cfg = {**cfg, "enabled": True}
    if not wake_surface_enabled(surface, cfg):
        # "disabled" (persist:true can turn it on) vs "disabled_for_surface" (explicit
        # wake_word.surface choice, which persist does NOT override).
        reason = "disabled" if not cfg.get("enabled") else "disabled_for_surface"
        logger.info("wake.start(%s): %s (enabled=%s, surface=%s)",
                    surface, reason, cfg.get("enabled"), cfg.get("surface"))
        return refused(reason)
    existing_owner, existing_surface = _wake_owner_snapshot()
    if existing_owner is not None and (_transport_is_dead(existing_owner) or not owns_listener(existing_owner)):
        if _release_wake_for_transport(existing_owner):
            existing_owner, existing_surface = None, ""
    if existing_owner is not None and existing_owner is not transport:
        return refused("owned", owner_surface=existing_surface)
    try:
        on_detect = _wake_detect_handler(transport, str(params.get("session_id") or ""),
                                         wake_phrase(cfg), bool(cfg.get("start_new_session", True)))
        start_listening(on_detect, owner=transport, config=cfg,
                        external_audio=capture_mode == "client")
    except WakeWordInUse:
        return refused("owned", owner_surface=existing_surface or None)
    except Exception as e:
        logger.warning("wake.start(%s): failed to start listener: %s", surface, e)
        return _err(rid, 5026, str(e))
    with _wake_lock:
        _wake_owner_transport, _wake_owner_surface = transport, surface
    frame = detector_frame_info()
    logger.info("wake.start(%s): listening for %r (%s) capture=%s frame=%s",
                surface, reqs["phrase"], reqs["provider"], capture_mode, frame.get("frame_length"))
    return _ok(rid, {
        "started": True, "phrase": reqs["phrase"], "provider": reqs["provider"],
        "owner_surface": surface, "enabled_persisted": enabled_persisted, "capture": capture_mode,
        **_frame_fields(frame)})


@method("wake.stop")
def _(rid, params: dict) -> dict:
    """Stop this surface's listener; ``persist: true`` also writes ``wake_word.enabled: false``."""
    stopped = _release_wake_for_transport(_caller_transport())
    disabled_persisted = False
    if params.get("persist"):
        try:
            from tools.wake_word import load_wake_word_config
            currently_enabled = bool(load_wake_word_config().get("enabled"))
        except Exception:
            currently_enabled = True
        disabled_persisted = currently_enabled and _persist_wake_enabled(False)
    return _owner_result(
        rid, "stopped", stopped,
        disabled_persisted=disabled_persisted,
        released=stopped,
        release_complete=stopped,
    )


@method("wake.pause")
def _(rid, params: dict) -> dict:
    """Release the mic (e.g. while the desktop's browser captures audio)."""
    try:
        from tools.wake_word import pause_listening
        paused = pause_listening(owner=_caller_transport())
        logger.info("wake.pause: detector paused=%s", paused)
    except Exception as e:
        logger.debug("wake.pause failed: %s", e)
        paused = False
    return _owner_result(
        rid, "paused", paused, released=paused, release_complete=paused)


@method("wake.resume")
def _(rid, params: dict) -> dict:
    """Reclaim the mic after a pause; no-op if the listener isn't armed."""
    resumed = _wake_resume_if_owner(_caller_transport())
    logger.info("wake.resume: detector resumed=%s", resumed)
    return _owner_result(
        rid, "resumed", resumed,
        released=not resumed,
        release_complete=not resumed,
    )


@method("wake.status")
def _(rid, params: dict) -> dict:
    try:
        from tools.wake_word import (
            audio_is_silent, detector_frame_info, get_input_device_status, is_listening,
            load_wake_word_config, owns_listener, silent_audio_hint)
        cfg = load_wake_word_config()
        probe_capture, reqs = _wake_probe(cfg, params, str(params.get("surface") or "").strip().lower())
        owner, owner_surface = _wake_owner_snapshot()
        owned_by_caller = owns_listener(_caller_transport())
        listening = owned_by_caller and is_listening()
        silent = listening and audio_is_silent()
        input_device = get_input_device_status(cfg)
        hint = reqs.get("hint", "")
        if input_device.get("error") and not hint:
            hint = f"Wake-word input device could not be resolved: {input_device['error']}"
        if silent and not hint:
            hint = silent_audio_hint(input_device)
        # Effective capture: prefer the *armed* detector over config/auto, else with capture:auto
        # a bare status probe reports "local" and the desktop never reattaches the PCM feeder.
        frame = detector_frame_info()
        if owned_by_caller and (frame.get("external_audio") or listening):
            capture = "client" if frame.get("external_audio") else "local"
        else:
            capture = probe_capture or reqs.get("capture") or str(cfg.get("capture") or "auto")
        # `enabled` is config truth (clients re-arm after a voice turn from it); `audio_silent` =
        # armed but deaf despite an open stream (see the platform-specific hint).
        return _ok(rid, {
            "listening": listening, "owned_by_caller": owned_by_caller,
            "owner_surface": owner_surface if owner is not None else None,
            "phrase": reqs["phrase"], "provider": reqs["provider"],
            "configured_surface": str(cfg.get("surface") or "auto"),
            "input_device": input_device, "available": reqs["available"], "hint": hint,
            "enabled": bool(cfg.get("enabled")), "audio_silent": silent, "capture": capture,
            "local_input_available": bool(reqs.get("local_input_available")), **_frame_fields(frame)})
    except Exception as e:
        return _err(rid, 5026, str(e))


@method("wake.diagnostics")
def _(rid, params: dict) -> dict:
    """Return resettable owner-only wake measurements without captured audio."""
    reset = params.get("reset", False)
    if not isinstance(reset, bool):
        return _err(rid, 4001, "wake.diagnostics reset must be boolean")
    try:
        from tools.wake_word import get_wake_diagnostics
        return _ok(rid, get_wake_diagnostics(owner=_caller_transport(), reset=reset))
    except Exception as exc:
        logger.debug("wake.diagnostics failed: %s", exc)
        return _err(rid, 5026, str(exc))


@method("wake.feed")
def _(rid, params: dict) -> dict:
    """Push client-captured PCM (``pcm``/``pcm_b64``: base64 int16 mono LE, 16 kHz only) into the
    armed detector (``capture: "client"``) — mic-less remote backends can run openWakeWord."""
    raw_b64 = params.get("pcm") or params.get("pcm_b64") or ""
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        return _err(rid, 4001, "wake.feed requires base64 pcm")
    import base64
    try:
        pcm = base64.b64decode(raw_b64, validate=False)
    except Exception as e:
        return _err(rid, 4001, f"invalid base64 pcm: {e}")
    if not pcm:
        return _ok(rid, {"fed": False, "reason": "empty"})
    if len(pcm) > 64000:  # soft cap: 2s of 16 kHz int16 mono
        return _err(rid, 4001, "pcm frame too large")
    if params.get("sample_rate") is not None and int(params["sample_rate"]) not in (0, 16000):
        return _err(rid, 4001, "wake.feed only accepts 16 kHz PCM")
    try:
        from tools.wake_word import feed_audio
        ok = feed_audio(owner=_caller_transport(), pcm_int16=pcm)
    except Exception as e:
        logger.debug("wake.feed failed: %s", e)
        return _err(rid, 5026, str(e))
    return _owner_result(rid, "fed", bool(ok))


def _voice_toggle_status(rid, params: dict) -> dict:
    # Mirrors CLI _show_voice_status: STT/TTS availability tells the user WHY voice isn't
    # working; record_key lets the TUI bind and display the shortcut.
    payload = _voice_status_payload()
    try:
        from tools.voice_mode import check_voice_requirements
        reqs = check_voice_requirements()
        payload.update({k: bool(reqs.get(k)) for k in ("available", "audio_available", "stt_available")},
                       details=reqs.get("details") or "")
    except Exception as e:
        # Optional transcription deps — /voice status must always answer.
        logger.warning("voice.toggle status: requirements probe failed: %s", e)
    return _ok(rid, payload)


def _voice_toggle_mode(rid, params: dict) -> dict:
    enabled = params.get("action") == "on"
    stop_hint = ""
    if enabled:
        transport = _caller_transport()
        sid = str(params.get("session_id") or "")
        if not sid:
            owner, _epoch, owner_transport, _capture_id = _voice_scope_snapshot()
            if owner_transport is transport:
                sid = owner
        if not sid:
            with _sessions_lock:
                candidates = [
                    candidate for candidate, record in _sessions.items()
                    if record.get("transport") is transport
                ]
            if len(candidates) == 1:
                sid = candidates[0]
        sid = sid or "__local_voice__"
        epoch = _acquire_voice_lease(sid, transport)
        if epoch is None:
            return _err(rid, 4016, "voice input is busy in another session")
        os.environ["HERMES_VOICE"] = "1"
        # Spoken-stop hint for the client; sourced from voice.stop_phrases, empty when disabled.
        with contextlib.suppress(Exception):
            from tools.voice_mode_transcript import voice_stop_hint
            stop_hint = voice_stop_hint()
        # Speech output already on → warm the engine now, not on the first reply.
        if _voice_tts_enabled():
            _tts_lease_async("tui:voice-tts", True)
    else:
        owner, epoch, transport, capture_id = _voice_scope_snapshot()
        caller, sid = _caller_transport(), str(params.get("session_id") or owner or "")
        if owner:
            if sid != owner or caller is not transport:
                return _err(rid, 4016, "voice capture is owned by another session")
            released, error = _release_voice_lease(
                session_id=owner, lease_epoch=epoch, transport=transport, stop_loop=True,
                before_expose=_voice_toggle_off_cleanup)
            if not released:
                from tools.voice_input import VoiceInputReleaseError
                _voice_terminal_error_epochs.add(epoch)
                _voice_emit(
                    "voice.input_error",
                    _input_error_payload(VoiceInputReleaseError(error), released=False),
                    session_id=owner, lease_epoch=epoch, transport=transport,
                    capture_id=capture_id)
                return _err(rid, 5025, error)
        else:
            try:
                from hermes_cli.voice import stop_continuous
                stop_continuous()
            except ImportError:
                pass
            except Exception as e:
                return _err(rid, 5025, str(e))
            _voice_toggle_off_cleanup()
        return _ok(rid, _voice_result_scope(
            session_id="", lease_epoch=epoch, capture_id=None,
            enabled=False, record_key=_voice_status_payload()["record_key"], tts=False,
            stop_hint=stop_hint, released=True, release_complete=True))
    return _ok(rid, _voice_status_payload(stop_hint=stop_hint))


def _set_voice_tts(on: bool) -> None:
    """Flip TTS; off silences live speech. The lease pre-loads the engine (on) / releases it (off)."""
    os.environ["HERMES_VOICE_TTS"] = "1" if on else "0"
    if not on:
        _tts_stream_stop(user_barge=False)
    _tts_lease_async("tui:voice-tts", on)


def _voice_toggle_off_cleanup() -> None:
    """No-throw cleanup that runs before an old owner lease becomes acquirable."""
    os.environ["HERMES_VOICE"] = "0"
    with contextlib.suppress(Exception):
        _cancel_output_drain_monitor()
    with contextlib.suppress(Exception):
        _set_voice_tts(False)  # TTS is toggled independently later
    with _fd_listener_lock:
        for stop, _done in _fd_speak_pipelines:
            stop.set()


def _voice_toggle_tts(rid, params: dict) -> dict:
    if not _voice_mode_enabled():
        return _err(rid, 4014, "enable voice mode first: /voice on")
    _set_voice_tts(not _voice_tts_enabled())
    return _ok(rid, _voice_status_payload())


_VOICE_TOGGLE_ACTIONS = {
    "status": _voice_toggle_status, "on": _voice_toggle_mode, "off": _voice_toggle_mode,
    "tts": _voice_toggle_tts}


@method("voice.toggle")
def _(rid, params: dict) -> dict:
    """CLI parity for ``/voice``: ``status``; ``on``/``off`` flip voice *mode* (off also tears
    down the continuous loop); ``tts`` toggles speech output (requires mode on)."""
    action = params.get("action", "status")
    handler = _VOICE_TOGGLE_ACTIONS.get(action) if isinstance(action, str) else None
    if handler is None:
        return _err(rid, 4013, f"unknown voice action: {action}")
    return handler(rid, params)


def _vr_emit(event: str, payload: dict, scope) -> bool:
    sid, epoch, transport, capture_id = scope
    return _voice_emit(
        event, payload, session_id=sid, lease_epoch=epoch, transport=transport,
        capture_id=capture_id)


def _vr_on_input_error(error, scope) -> None:
    """Terminal provider/STT failure: release first, then report scoped truth."""
    sid, epoch, transport, _capture_id = scope
    _voice_terminal_error_epochs.add(epoch)
    released, release_error = _release_voice_lease(
        session_id=sid, lease_epoch=epoch, transport=transport, stop_loop=True)
    _vr_emit("voice.input_error", _input_error_payload(error, released=released), scope)
    if not released and str(getattr(error, "stage", "")) != "close":
        from tools.voice_input import VoiceInputReleaseError
        _vr_emit(
            "voice.input_error",
            _input_error_payload(VoiceInputReleaseError(release_error), released=False),
            scope)
    if released:
        _resume_voice_wake()


def _vr_on_status(state, scope) -> None:
    global _voice_pending_idle_scope, _voice_release_tail_epoch
    sid, epoch, transport, capture_id = scope
    if state == "idle":
        with _voice_sid_lock:
            releasing = epoch == _voice_lease_epoch and _voice_lease_releasing
            if releasing:
                _voice_pending_idle_scope = scope
                return
        if epoch in _voice_terminal_error_epochs:
            return
        released, error = _release_voice_lease(
            session_id=sid, lease_epoch=epoch, transport=transport, stop_loop=False)
        if not released:
            from tools.voice_input import VoiceInputReleaseError
            _voice_terminal_error_epochs.add(epoch)
            _vr_emit(
                "voice.input_error",
                _input_error_payload(VoiceInputReleaseError(error), released=False), scope)
            return
        _voice_emit(
            "voice.status",
            {"state": state, "outcome": "complete", "released": True,
             "release_complete": True},
            session_id=sid, lease_epoch=epoch, transport=transport,
            capture_id=capture_id)
        with _voice_sid_lock:
            if _voice_release_tail_epoch == epoch:
                _voice_release_tail_epoch = None
        _resume_voice_wake()
        return
    _voice_emit(
        "voice.status",
        {"state": state, "released": False, "release_complete": False},
        session_id=sid, lease_epoch=epoch, transport=transport,
        capture_id=capture_id)


def _vr_on_partial(text: str, scope) -> None:
    sid, epoch, transport, capture_id = scope
    _voice_emit(
        "voice.partial", {"text": text},
        session_id=sid, lease_epoch=epoch, transport=transport,
        capture_id=capture_id)


def _vr_on_stop_phrase(text, scope) -> None:
    sid, _epoch, _transport, _capture_id = scope
    if _end_session_voice_chat(sid, stop_loop=True, stop_tts=True):
        _vr_emit("voice.transcript", {"stop_phrase": True, "text": text}, scope)


def _vr_on_silent_limit(scope) -> None:
    sid, _epoch, _transport, _capture_id = scope
    if _end_session_voice_chat(sid, stop_loop=True, stop_tts=False):
        _vr_emit("voice.transcript", {"no_speech_limit": True}, scope)


@method("voice.record")
def _(rid, params: dict) -> dict:
    """VAD-bounded push-to-talk. ``start`` emits ``voice.transcript`` when silence stops the
    capture; ``stop`` forces transcription. Three silent captures emit ``no_speech_limit``."""
    action = params.get("action", "start")
    wake_paused = False
    if action not in {"start", "stop"}:
        return _err(rid, 4019, f"unknown voice action: {action}")
    transport = _caller_transport()
    wake_owner, _surface = _wake_owner_snapshot()
    if wake_owner is not None and wake_owner is not transport:
        return _ok(rid, {"status": "busy", "reason": "wake_owned"})
    try:
        global _voice_event_sid, _voice_event_transport, _voice_wake_owner, _voice_capture_sid
        sid = str(params.get("session_id") or "")
        if action == "start" and not _session_voice_enabled(sid):
            return _err(rid, 4015, "voice mode is off — enable with /voice on")
        owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        if action == "stop":
            if not owner:
                # Compatibility for an already-running native capture opened
                # before scoped leasing (and for older clients that only send
                # stop): fence it now, then perform the same physical close.
                epoch = _acquire_voice_lease(sid, transport)
                if epoch is None:
                    return _err(rid, 4016, "voice capture is busy in another session")
                owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
                with _voice_sid_lock:
                    _voice_event_sid = sid or _voice_event_sid
                    _voice_event_transport = transport
            if sid != owner or transport is not owner_transport:
                return _err(rid, 4016, "voice capture is owned by another session")
            _mark_voice_release_tail(epoch)
            released, error = _release_voice_lease(
                session_id=owner, lease_epoch=epoch, transport=owner_transport,
                stop_loop=True, force_transcribe=True)
            if not released:
                from tools.voice_input import VoiceInputReleaseError
                _voice_terminal_error_epochs.add(epoch)
                _voice_emit(
                    "voice.input_error",
                    _input_error_payload(VoiceInputReleaseError(error), released=False),
                    session_id=owner, lease_epoch=epoch, transport=owner_transport,
                    capture_id=capture_id)
                return _err(rid, 5025, error)
            _resume_voice_wake()
            return _ok(rid, _voice_result_scope(
                session_id=owner, lease_epoch=epoch, capture_id=capture_id,
                status="stopped", released=True, release_complete=True))

        capture_live = _capture_live()
        if owner and not capture_live:
            with _fd_listener_lock:
                full_duplex_active = _fd_listener_active
            placeholder_handoff = (
                owner == "__local_voice__" and owner_transport is transport
            )
            same_scope = owner == sid and owner_transport is transport
            if not full_duplex_active and not placeholder_handoff and not same_scope:
                _release_voice_lease(
                    session_id=owner, lease_epoch=epoch, transport=owner_transport,
                    stop_loop=False)
                owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        unleased_capture = owner_transport is None and capture_live
        requested_capture = capture_id if owner else uuid.uuid4().hex
        epoch = _acquire_voice_lease(sid, transport, capture_id=requested_capture)
        if epoch is None:
            return _err(rid, 4016, "voice capture is busy in another session")
        owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        scope = (owner, epoch, owner_transport, capture_id)
        with _voice_sid_lock:
            _voice_event_sid = sid or _voice_event_sid
            _voice_event_transport = transport
            _voice_capture_sid = sid
        # PTT barge-in (#40010): arming the mic cuts in-flight TTS so a stale
        # reply can't talk over the user — mirrors the CLI record-key handler.
        # user_barge=True also marks the speech interrupted for the next turn's
        # model note, and stop_playback() releases the file player.
        _tts_stream_stop(user_barge=True)
        from hermes_cli.voice import start_continuous
        # Busy probe holds the no-speech counter during long agent turns; safe to re-register every
        # start (older wrappers lack the setter).
        with contextlib.suppress(Exception):
            from hermes_cli.voice import set_voice_busy_probe
            set_voice_busy_probe(_any_session_running)
        # Shape-safe: malformed voice YAML falls back to documented defaults; an explicit numeric
        # max_recording_seconds <= 0 disables the cap (0.0).
        # Shape-safe lookups: malformed ``voice:`` YAML (bool/scalar/list) must not crash /voice with a 5025
        # — fall back to VAD defaults. Exclude ``bool`` from the numeric check since Python's bool is a
        # subclass of int — a hand-edit like ``silence_threshold: true`` would otherwise forward as ``1``
        # instead of falling back to the documented 200 / 3.0 defaults (Copilot round-12 on #19835).
        voice_cfg = _voice_cfg_dict()
        max_rec = _voice_cfg_number(voice_cfg.get("max_recording_seconds"), 120.0)
        def live(fn):
            return _voice_capture_callback(
                fn, session_id=owner, lease_epoch=epoch, transport=owner_transport)

        with _voice_input_start_barrier(owner, epoch, owner_transport) as can_start:
            if not can_start:
                return _err(rid, 4016, "voice input release is in progress")
            if unleased_capture:
                # A legacy/unscoped loop keeps its original callbacks when
                # start_continuous sees it active. Close it completely before
                # this lease installs any session-scoped callbacks.
                from hermes_cli.voice import stop_continuous
                stop_continuous()
                from tools.voice_input import release_voice_input
                release_voice_input(timeout=_voice_release_timeout(), exclusive_only=False)
                if _capture_live():
                    from tools.voice_input import VoiceInputReleaseError
                    raise VoiceInputReleaseError("unleased voice capture did not stop")
            # Hand the mic to STT if the wake detector holds it; a terminal capture event resumes it.
            with contextlib.suppress(Exception):
                from tools.wake_word import pause_listening
                wake_paused = pause_listening(owner=transport)
            if wake_paused:
                with _voice_sid_lock:
                    _voice_wake_owner = transport
            started = start_continuous(
                on_transcript=live(lambda text: _vr_emit("voice.transcript", {"text": text}, scope)),
                on_status=live(lambda state: _vr_on_status(state, scope)),
                on_silent_limit=live(lambda: _vr_on_silent_limit(scope)),
                silence_threshold=_voice_cfg_number(voice_cfg.get("silence_threshold"), 200),
                silence_duration=_voice_cfg_number(voice_cfg.get("silence_duration"), 3.0),
                auto_restart=False, max_recording_seconds=max_rec if max_rec > 0 else 0.0,
                on_stop_phrase=live(lambda text: _vr_on_stop_phrase(text, scope)),
                on_partial=live(lambda text: _vr_on_partial(text, scope)),
                on_input_error=live(lambda error: _vr_on_input_error(error, scope)))
        if started is False:
            released, release_error = _release_voice_lease(
                session_id=owner, lease_epoch=epoch,
                transport=owner_transport, stop_loop=True)
            if not released:
                from tools.voice_input import VoiceInputReleaseError
                error = VoiceInputReleaseError(release_error)
                _voice_emit(
                    "voice.input_error", _input_error_payload(error, released=False),
                    session_id=owner, lease_epoch=epoch, transport=owner_transport,
                    capture_id=capture_id)
                return _err(rid, 5025, release_error)
            if wake_paused:
                _resume_voice_wake()
        return _ok(rid, _voice_result_scope(
            session_id=owner, lease_epoch=epoch, capture_id=capture_id,
            status="busy" if started is False else "recording",
            released=started is False, release_complete=started is False))
    except Exception as e:
        owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        released = False
        if owner and owner_transport is transport:
            released, release_error = _release_voice_lease(
                session_id=owner, lease_epoch=epoch, transport=owner_transport, stop_loop=True)
            from tools.voice_input import VoiceInputError, VoiceInputReleaseError
            input_error = e if isinstance(e, VoiceInputError) else VoiceInputError(
                str(e), stage="open", retryable=True)
            _voice_emit(
                "voice.input_error", _input_error_payload(input_error, released=released),
                session_id=owner, lease_epoch=epoch, transport=owner_transport,
                capture_id=capture_id)
            if not released:
                _voice_emit(
                    "voice.input_error",
                    _input_error_payload(VoiceInputReleaseError(release_error), released=False),
                    session_id=owner, lease_epoch=epoch, transport=owner_transport,
                    capture_id=capture_id)
        if released and wake_paused:
            _resume_voice_wake()
        if isinstance(e, ImportError):
            return _err(rid, 5025, "voice module not available — install audio dependencies")
        return _err(rid, 5025, str(e))


@method("voice.tts")
def _(rid, params: dict) -> dict:
    text = params.get("text", "")
    if not text:
        return _err(rid, 4020, "text required")
    owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
    sid, caller = str(params.get("session_id") or owner or ""), _caller_transport()
    if owner and (sid != owner or caller is not owner_transport):
        return _err(rid, 4016, "voice output is owned by another session")
    try:
        import hermes_cli.voice
    except Exception as e:
        return _err(rid, 5026, "voice module not available" if isinstance(e, ImportError) else str(e))
    if not owner and _voice_mode_enabled() and _voice_cfg_dict().get("barge_in", True):
        scope = _full_duplex_scope(sid, caller)
        if scope is None:
            return _err(rid, 4016, "voice input is busy in another session")
        owner, epoch, owner_transport = scope
    threading.Thread(
        target=_speak_text_with_barge,
        args=(text, owner or sid, owner_transport or caller), daemon=True).start()
    return _ok(rid, _voice_result_scope(
        session_id=owner or sid, lease_epoch=epoch if owner else 0,
        capture_id=capture_id, status="speaking"))


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
