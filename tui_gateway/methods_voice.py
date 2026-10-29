"""Voice / TTS / wake-word JSON-RPC facade (one mic and speaker per process).

The physical lease/authority lifecycle lives in ``methods_voice_lease``; both
modules rebind their bodies onto ``server.py`` globals through ``method_ctx``.
"""

from __future__ import annotations

import contextlib
import threading
import uuid

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


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


def _stop_session_voice_capture(
    session_id, *, transport=None, lease_epoch: int | None = None,
) -> bool:
    """Release *session_id*'s capture without touching another owner's input."""
    with _voice_lifecycle_lock:
        return _stop_session_voice_capture_guarded(
            session_id, transport=transport, lease_epoch=lease_epoch
        )


def _stop_session_voice_capture_guarded(
    session_id, *, transport=None, lease_epoch: int | None = None,
) -> bool:
    """Stop the captured wrapper while successor claim/start remains fenced."""
    global _voice_capture_gen
    with _voice_sid_lock:
        owner = _voice_lease_owner_sid
        epoch = _voice_lease_epoch
        owner_transport = _voice_lease_owner_transport
        capture_id = _voice_lease_capture_id
        if not owner:
            captured_transport = _voice_event_transport
            captured_unowned = (
                bool(session_id)
                and session_id == _voice_capture_sid
                and captured_transport is not None
                and (transport is None or transport is captured_transport)
                and (lease_epoch is None or lease_epoch == epoch)
            )
            if captured_unowned:
                # The real recorder closes its provider before delivering the
                # transcript, but its logical loop remains active until the
                # subsequent idle callback.  A session-local /voice off in
                # that gap must still invalidate and stop that captured loop.
                _voice_capture_gen += 1
        else:
            captured_unowned = False
        caller = owner_transport if transport is None else transport
        if owner and (
            not session_id
            or session_id != owner
            or caller is not owner_transport
            or (lease_epoch is not None and lease_epoch != epoch)
            or _current_voice_profile_home() != _voice_lease_profile_home
        ):
            return False
        if owner:
            _voice_capture_gen += 1
    if not owner:
        if not captured_unowned or _capture_active_state() is False:
            return True
        from hermes_cli.voice import stop_continuous
        stop_continuous()
        return not _capture_live()
    released, error = _release_voice_lease(
        session_id=owner, lease_epoch=epoch, transport=owner_transport, stop_loop=True,
        before_expose=lambda: _resume_voice_wake(
            expected_transport=owner_transport, expected_epoch=epoch))
    if not released:
        from tools.voice_input import VoiceInputReleaseError
        _voice_terminal_error_epochs.add(epoch)
        _voice_emit(
            "voice.input_error",
            _input_error_payload(VoiceInputReleaseError(error), released=False),
            session_id=owner, lease_epoch=epoch, transport=owner_transport,
            capture_id=capture_id)
        return False
    return True


def _capture_active_state() -> bool | None:
    try:
        from hermes_cli.voice import is_continuous_active
    except ImportError:  # an older voice wrapper without the probe
        return None
    return bool(is_continuous_active())


def _capture_live() -> bool:
    return bool(_capture_active_state())


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
    from agent.memory_provider import ctx_bound

    return ctx_bound(call)


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


def _end_session_voice_chat(
    session_id, *, stop_loop: bool, stop_tts: bool, transport=None,
    lease_epoch: int | None = None,
) -> bool:
    """A stop phrase (spoken, typed or full-duplex) ends *session_id*'s voice chat: a shared-owner
    session through its own mode (which ends only its capture); else the sidecar's env flags."""
    sid = str(session_id or "")
    caller = transport or _caller_transport()
    _authority_sid, authority_transport = _voice_authority_snapshot()
    if stop_loop:
        _owner, _epoch, owner_transport, _capture_id = _voice_scope_snapshot()
        legacy_capture = owner_transport is None and _capture_live()
        claimed_epoch = _claim_voice_authority(
            sid,
            caller,
            allow_unclaimed=authority_transport is None,
            release_all=legacy_capture,
            expected_epoch=lease_epoch,
        )
        if claimed_epoch is None:
            return False
    elif not _voice_control_scope_accepts(sid, caller):
        return False
    if stop_loop and not _stop_session_voice_capture(
        sid, transport=caller, lease_epoch=lease_epoch or claimed_epoch
    ):
        return False
    if not _set_session_voice_off(sid):
        _end_voice_chat(stop_loop=False, stop_tts=stop_tts)
    elif stop_tts:
        with contextlib.suppress(Exception):
            _tts_stream_stop(user_barge=False)
    _clear_voice_authority(session_id=sid, transport=caller)
    return True


def _tts_lease_async(lease: str, active: bool) -> None:
    """Acquire/release a TTS lease off the RPC thread (acquiring warms a local engine; must not
    block the toggle's reply). Best-effort."""
    global _voice_tts_lease_generation
    with _voice_sid_lock:
        _voice_tts_lease_generation += 1
        generation = _voice_tts_lease_generation
        _voice_tts_lease_desired[lease] = (generation, active)

    def _run():
        try:
            from tools.tts_tool_lifecycle import acquire_tts_lease, release_tts_lease
            with _voice_tts_lease_lock:
                with _voice_sid_lock:
                    if _voice_tts_lease_desired.get(lease) != (generation, active):
                        return
                (acquire_tts_lease if active else release_tts_lease)(lease)
        except Exception as e:
            logger.debug("voice: tts lease %s active=%s failed: %s", lease, active, e)
    _start_voice_thread(_run, name=f"tts-lease-{lease}")


def _running_sessions() -> list:
    with _sessions_lock:
        return [s for s in _sessions.values() if s.get("running")]


def _any_session_running() -> bool:
    """Voice busy-probe: silent captures during a long turn don't count toward the no-speech limit."""
    try:
        return bool(_running_sessions())
    except Exception:
        return False


def _voice_output_bound_transport(transport):
    """Resolve a session fanout to its installed exact voice owner.

    ``FanoutTransport`` is only the session's event router.  Membership may
    preserve an existing physical/logical owner, but the router itself never
    becomes voice authority.
    """
    _owner, _epoch, lease_transport, _capture_id = _voice_scope_snapshot()
    _authority_sid, authority_transport = _voice_authority_snapshot()
    expected = lease_transport or authority_transport
    if expected is None:
        return transport
    if bool(getattr(expected, "_closed", False)):
        return None
    if transport is expected:
        return expected
    if isinstance(transport, FanoutTransport) and transport.contains(expected):
        return expected
    return None


def _voice_output_scope_allowed(
    session_id: str | None, transport, *, require_voice_scope: bool = False,
) -> bool:
    """Allow turn audio only for the exact lease owner, claiming an idle voice lease if needed."""
    if bool(getattr(transport, "_closed", False)):
        return False
    _owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
    if owner_transport is not None:
        requested_transport = _voice_output_bound_transport(transport)
        if not session_id or requested_transport is None:
            return False
        acquired = _acquire_voice_lease(str(session_id), requested_transport)
        return acquired == epoch and _voice_scope_matches(
            str(session_id), epoch, requested_transport)
    authority_sid, authority_transport = _voice_authority_snapshot()
    if authority_transport is not None:
        sid = str(session_id or authority_sid)
        requested_transport = _voice_output_bound_transport(
            transport or authority_transport
        )
        if requested_transport is None:
            return False
        return _voice_authority_accepts(
            sid, requested_transport, allow_unclaimed=False
        )
    if require_voice_scope:
        return False
    if not _voice_mode_enabled():
        return True  # native TTS-only behavior without a voice/microphone lease
    return False


@contextlib.contextmanager
def _voice_output_start_barrier(
    session_id: str | None, transport, *, require_voice_scope: bool = False,
):
    """Fence output startup against release cleanup and successor acquisition."""
    with _voice_lifecycle_lock:
        yield _voice_output_scope_allowed(
            session_id, transport, require_voice_scope=require_voice_scope
        )


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
        _start_voice_thread(
            stream_tts_to_speaker,
            name="voice-tts-stream",
            args=(text_queue, stop, done),
        )
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
_fd_listener_generation = 0
_fd_speak_pipelines: set[tuple[threading.Event, threading.Event]] = set()
_output_drain_lock = threading.Lock()
_output_drain_stop: threading.Event | None = None
_output_drain_epoch = 0


def _full_duplex_scope(session_id: str | None = None, transport=None):
    """Resolve or acquire the one logical microphone lease for a listener."""
    owner, epoch, owner_transport, _capture_id = _voice_scope_snapshot()
    if owner_transport is not None:
        sid = str(session_id or owner)
        requested_transport = transport or owner_transport
        if sid != owner and not (
            owner == "__local_voice__" and requested_transport is owner_transport
        ):
            return None
        if requested_transport is not owner_transport:
            return None
        promoted = _claim_voice_authority(
            sid,
            owner_transport,
            allow_unclaimed=True,
        )
        return (sid, promoted, owner_transport, None) if promoted is not None else None

    authority_sid, authority_transport = _voice_authority_snapshot()
    if authority_transport is None:
        return None
    sid = str(session_id or authority_sid)
    requested_transport = transport or authority_transport
    previous_epoch = epoch
    epoch = _claim_voice_authority(
        sid,
        requested_transport,
        allow_unclaimed=False,
    )
    if epoch is None:
        return None
    return sid, epoch, requested_transport, (
        previous_epoch if previous_epoch > 0 and previous_epoch != epoch else None
    )


def _emit_voice_lease_changed(
    session_id: str,
    lease_epoch: int,
    transport,
    previous_lease_epoch: int | None,
    phase: str,
) -> bool:
    if previous_lease_epoch is None or previous_lease_epoch == lease_epoch:
        return False
    return _voice_emit(
        "voice.lease_changed",
        {"previous_lease_epoch": previous_lease_epoch, "phase": phase},
        session_id=session_id,
        lease_epoch=lease_epoch,
        transport=transport,
    )


def _arm_full_duplex_listener(session_id: str | None = None, transport=None) -> bool:
    """Arm one full-duplex listener with an immutable session/epoch/transport scope."""
    with _voice_lifecycle_lock:
        scope = _full_duplex_scope(session_id, transport)
        if scope is None:
            return False
        _cancel_output_drain_monitor()
        sid, epoch, owner_transport, previous_epoch = scope
        global _fd_listener_active, _fd_listener_epoch, _fd_listener_generation
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
            _fd_listener_generation += 1
            listener_generation = _fd_listener_generation
        _emit_voice_lease_changed(
            sid, epoch, owner_transport, previous_epoch, "full_duplex"
        )

        def _run_listener(listener_sid, listener_epoch, listener_transport):
            _full_duplex_listener(
                listener_sid, listener_epoch, listener_transport, listener_generation
            )

        _start_voice_thread(
            _run_listener,
            name="voice-full-duplex",
            args=(sid, epoch, owner_transport),
        )
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
    global _output_drain_stop, _output_drain_epoch
    with _output_drain_lock:
        stop, _output_drain_stop = _output_drain_stop, None
        _output_drain_epoch = 0
    if stop is not None:
        stop.set()


def _arm_output_drain_monitor(session_id: str, transport=None) -> bool:
    """Emit one scoped output-drained event without opening the microphone."""
    global _output_drain_stop, _output_drain_epoch
    with _voice_lifecycle_lock:
        scope = _full_duplex_scope(session_id, transport)
        if scope is None or not _voice_mode_enabled():
            return False
        sid, epoch, owner_transport, previous_epoch = scope
        stop = threading.Event()
        with _output_drain_lock:
            if (
                _output_drain_stop is not None
                and not _output_drain_stop.is_set()
                and _output_drain_epoch == epoch
            ):
                return True
            previous, _output_drain_stop = _output_drain_stop, stop
            _output_drain_epoch = epoch
        if previous is not None:
            previous.set()
        _emit_voice_lease_changed(
            sid, epoch, owner_transport, previous_epoch, "output_drain"
        )
        _start_voice_thread(
            _output_drain_monitor,
            name="voice-output-drain",
            args=(sid, epoch, owner_transport, stop),
        )
    return True


def _output_drain_monitor(
    session_id: str, lease_epoch: int, transport, stop: threading.Event,
) -> None:
    """Wait until this turn and every backend audio pipeline are idle."""
    global _output_drain_stop, _output_drain_epoch
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
                _output_drain_epoch = 0
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
                _output_drain_epoch = 0


def _retire_full_duplex_listener(
    lease_epoch: int, listener_generation: int | None,
) -> None:
    """Retire only this listener instance; a same-epoch successor owns its slot."""
    global _fd_listener_active, _fd_listener_epoch
    with _fd_listener_lock:
        if _fd_listener_epoch == lease_epoch and (
            listener_generation is None
            or _fd_listener_generation == listener_generation
        ):
            _fd_listener_active = False
            _fd_listener_epoch = 0


def _full_duplex_listener(
    session_id: str,
    lease_epoch: int,
    transport,
    listener_generation: int | None = None,
) -> None:
    """Mic live from utterance-submit to turn-complete; a trip transcribes -> ``voice.transcript``."""
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
            stop_loop=False,
            before_expose=lambda: _resume_voice_wake(
                expected_transport=transport, expected_epoch=lease_epoch))
        _voice_emit(
            "voice.input_error", _input_error_payload(error, released=released),
            session_id=session_id, lease_epoch=lease_epoch, transport=transport)
        if not released and getattr(error, "stage", "") != "close":
            _voice_emit(
                "voice.input_error",
                _input_error_payload(VoiceInputReleaseError(release_error), released=False),
                session_id=session_id, lease_epoch=lease_epoch, transport=transport)

    from agent.memory_provider import ctx_bound
    on_input_error = ctx_bound(_on_input_error)

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
                                      grace_ms=grace_ms, on_input_error=on_input_error,
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
                on_input_error(VoiceInputError(
                    f"voice transcription failed: {exc}", stage="stt", retryable=True))
                return
            if result.get("success") and (result.get("transcript") or "").strip():
                _retire_full_duplex_listener(lease_epoch, listener_generation)
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
                on_input_error(VoiceInputError(
                    str(result.get("error") or "voice transcription failed"),
                    stage="stt", retryable=True))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(wav_path)
    except Exception as e:
        logger.debug("full-duplex listener failed: %s", e)
        on_input_error(e)
    finally:
        _retire_full_duplex_listener(lease_epoch, listener_generation)


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
        if not _end_session_voice_chat(
            session_id, stop_loop=True, stop_tts=False, transport=transport,
            lease_epoch=lease_epoch,
        ):
            return False
    return _voice_emit(
        "voice.transcript", {"stop_phrase": True, "text": text} if is_stop else {"text": text},
        session_id=session_id, lease_epoch=lease_epoch, transport=transport)


def _speak_text_with_barge(
    text: str,
    session_id: str | None = None,
    transport=None,
    require_voice_scope: bool = False,
) -> None:
    """speak_text registered in ``_fd_speak_pipelines`` so the listener can cut it / waits for it."""
    from hermes_cli.voice import speak_text
    with _voice_output_start_barrier(
        session_id, transport, require_voice_scope=require_voice_scope
    ) as allowed:
        if not allowed:
            return
        transport = _voice_output_bound_transport(transport)
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
        _start_voice_thread(_speak, name="voice-speak")
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
    owner, epoch, _transport, capture_id = _voice_scope_snapshot()
    if not owner:
        owner, _authority_transport = _voice_authority_snapshot()
    return _voice_result_scope(
        session_id=owner, lease_epoch=epoch, capture_id=capture_id,
        enabled=_voice_mode_enabled(), record_key=record_key, tts=_voice_tts_enabled(), **extra)


# ── Wake word ("Hey Hermes"): process-global detector (one mic). The first eligible transport
# to call wake.start owns it until stop, disconnect, or stream failure; on detection we emit
# wake.detected and the client opens a session + its own capture. The detector yields the mic
# to voice.record (pause/resume) and to the desktop's browser mic (wake.pause/resume RPCs).
_wake_lock = threading.Lock()
_wake_owner_transport: Optional[Transport] = None
_wake_owner_surface = ""
_wake_owner_profile_home: str | None = None


def _wake_owner_snapshot():
    with _wake_lock:
        return _wake_owner_transport, _wake_owner_surface


def _wake_profile_accepts(transport) -> bool:
    profile_home = _current_voice_profile_home()
    with _wake_lock:
        return (
            _wake_owner_transport is transport
            and _wake_owner_profile_home == profile_home
        )


def _release_wake_for_transport(
    transport: Transport, *, require_profile: bool = False,
) -> bool:
    """Release the wake lease iff ``transport`` is the current gateway owner."""
    global _wake_owner_transport, _wake_owner_surface, _wake_owner_profile_home
    profile_home = _current_voice_profile_home()
    with _wake_lock:
        if _wake_owner_transport is not transport:
            return False
        if require_profile and _wake_owner_profile_home != profile_home:
            return False
        try:
            from tools.wake_word import stop_listening
            stopped = bool(stop_listening(owner=transport))
        except Exception as e:
            logger.debug("wake stop failed: %s", e)
            return False
        if not stopped:
            return False
        _wake_owner_transport, _wake_owner_surface, _wake_owner_profile_home = None, "", None
        return True


def _release_gateway_wake_owner() -> bool:
    owner, _surface = _wake_owner_snapshot()
    return owner is not None and _release_wake_for_transport(owner)


_wake_resume_retry_lock = threading.Lock()
_wake_resume_retry_active = False
_wake_resume_retry_epoch: int | None = None


def _wake_resume_if_owner(owner: Transport, *, retry_seconds: float = 15.0,
                          retry_interval: float = 1.0,
                          expected_voice_epoch: int | None = None) -> bool:
    """Resume the wake detector for ``owner``, self-healing a busy microphone: reopening right after
    a voice turn can fail while the device is still being released (browser WebRTC tracks release
    async), so an exception retries in a background thread until it sticks, the lease changes hands,
    or ``retry_seconds`` elapses. ``False`` (lease gone / other owner) is final — never retried."""
    from tools.wake_word import resume_listening
    with _voice_sid_lock:
        resume_epoch = _voice_lease_epoch if expected_voice_epoch is None else expected_voice_epoch

    def resume_if_current() -> bool | None:
        # The lifecycle barrier serializes detector reopen with a successor's
        # physical start; the epoch rejects an old same-transport retry.
        with _voice_lifecycle_lock:
            with _voice_sid_lock:
                if _voice_lease_epoch != resume_epoch:
                    return None
            return bool(resume_listening(owner=owner))

    try:
        resumed = resume_if_current()
        return bool(resumed)
    except Exception as e:
        logger.debug("wake resume failed (will retry): %s", e)
    global _wake_resume_retry_active, _wake_resume_retry_epoch
    with _wake_resume_retry_lock:
        if _wake_resume_retry_active and _wake_resume_retry_epoch == resume_epoch:
            return False
        _wake_resume_retry_active = True
        _wake_resume_retry_epoch = resume_epoch

    def _retry() -> None:
        global _wake_resume_retry_active, _wake_resume_retry_epoch
        deadline = time.monotonic() + retry_seconds
        try:
            while time.monotonic() < deadline:
                time.sleep(retry_interval)
                with _wake_resume_retry_lock:
                    if _wake_resume_retry_epoch != resume_epoch:
                        return
                with contextlib.suppress(Exception):
                    resumed = resume_if_current()
                    if resumed is None:
                        return
                    if resumed:
                        logger.info("wake: detector resumed after retry")
                    return  # False — detector gone or lease moved: stop, don't fight it.
            logger.warning("wake: could not resume detector after voice turn "
                           "(microphone still busy?) — toggle the wake word to re-arm")
        finally:
            with _wake_resume_retry_lock:
                if _wake_resume_retry_epoch == resume_epoch:
                    _wake_resume_retry_active = False
                    _wake_resume_retry_epoch = None
    _start_voice_thread(_retry, name="wake-resume-retry")
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
    from agent.memory_provider import ctx_bound

    return ctx_bound(_on_detect)


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
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Arm the wake-word listener for the calling surface ("tui" | "gui"); ``{started: False,
    reason}`` when disabled/owned/not ready. ``persist: true`` (explicit gesture) also flips
    ``wake_word.enabled`` on; auto-arm callers omit it."""
    global _wake_owner_transport, _wake_owner_surface, _wake_owner_profile_home
    surface = str(params.get("surface") or "auto").strip().lower()
    transport = _caller_transport()
    profile_home = _current_voice_profile_home()

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
    with _wake_lock:
        existing_profile_home = _wake_owner_profile_home
    if existing_owner is not None and (_transport_is_dead(existing_owner) or not owns_listener(existing_owner)):
        if _release_wake_for_transport(existing_owner):
            existing_owner, existing_surface = None, ""
            existing_profile_home = None
    if existing_owner is not None and (
        existing_owner is not transport or existing_profile_home != profile_home
    ):
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
        _wake_owner_profile_home = profile_home
    frame = detector_frame_info()
    logger.info("wake.start(%s): listening for %r (%s) capture=%s frame=%s",
                surface, reqs["phrase"], reqs["provider"], capture_mode, frame.get("frame_length"))
    return _ok(rid, {
        "started": True, "phrase": reqs["phrase"], "provider": reqs["provider"],
        "owner_surface": surface, "enabled_persisted": enabled_persisted, "capture": capture_mode,
        **_frame_fields(frame)})


@method("wake.stop")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Stop this surface's listener; ``persist: true`` also writes ``wake_word.enabled: false``."""
    caller = _caller_transport()
    wake_owner, _surface = _wake_owner_snapshot()
    if wake_owner is not None and not _wake_profile_accepts(caller):
        return _owner_result(
            rid, "stopped", False, disabled_persisted=False,
            released=False, release_complete=False,
        )
    stopped = _release_wake_for_transport(caller, require_profile=True)
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
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Release the mic (e.g. while the desktop's browser captures audio)."""
    if not _wake_profile_accepts(_caller_transport()):
        return _owner_result(rid, "paused", False, released=False, release_complete=False)
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
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Reclaim the mic after a pause; no-op if the listener isn't armed."""
    if not _wake_profile_accepts(_caller_transport()):
        return _owner_result(rid, "resumed", False, released=True, release_complete=True)
    resumed = _wake_resume_if_owner(_caller_transport())
    logger.info("wake.resume: detector resumed=%s", resumed)
    return _owner_result(
        rid, "resumed", resumed,
        released=not resumed,
        release_complete=not resumed,
    )


@method("wake.status")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        from tools.wake_word import (
            audio_is_silent, detector_frame_info, get_input_device_status, is_listening,
            load_wake_word_config, owns_listener, silent_audio_hint)
        cfg = load_wake_word_config()
        probe_capture, reqs = _wake_probe(cfg, params, str(params.get("surface") or "").strip().lower())
        owner, owner_surface = _wake_owner_snapshot()
        owned_by_caller = (
            _wake_profile_accepts(_caller_transport())
            and owns_listener(_caller_transport())
        )
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
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Return resettable owner-only wake measurements without captured audio."""
    reset = params.get("reset", False)
    if not isinstance(reset, bool):
        return _err(rid, 4001, "wake.diagnostics reset must be boolean")
    if not _wake_profile_accepts(_caller_transport()):
        return _err(rid, 4016, "wake input is owned by another profile")
    try:
        from tools.wake_word import get_wake_diagnostics
        return _ok(rid, get_wake_diagnostics(owner=_caller_transport(), reset=reset))
    except Exception as exc:
        logger.debug("wake.diagnostics failed: %s", exc)
        return _err(rid, 5026, str(exc))


@method("wake.feed")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Push client-captured PCM (``pcm``/``pcm_b64``: base64 int16 mono LE, 16 kHz only) into the
    armed detector (``capture: "client"``) — mic-less remote backends can run openWakeWord."""
    if not _wake_profile_accepts(_caller_transport()):
        return _owner_result(rid, "fed", False)
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
        with _voice_sid_lock:
            if _voice_lease_releasing:
                return _err(rid, 4016, "voice input is busy in another session")
        with _voice_lifecycle_lock:
            epoch = _claim_voice_authority(
                sid, transport, allow_unclaimed=True,
            )
            if epoch is None:
                return _err(rid, 4016, "voice input is busy in another session")
            with _voice_sid_lock:
                current = (
                    not _voice_lease_releasing
                    and _voice_lease_epoch == epoch
                    and _voice_lease_owner_sid == sid
                    and _voice_lease_owner_transport is transport
                    and _voice_authority_sid == sid
                    and _voice_authority_transport is transport
                )
                if current:
                    os.environ["HERMES_VOICE"] = "1"
            if not current:
                return _err(rid, 4016, "voice input is busy in another session")
            if _voice_tts_enabled():
                _tts_lease_async("tui:voice-tts", True)
        # Spoken-stop hint for the client; sourced from voice.stop_phrases, empty when disabled.
        with contextlib.suppress(Exception):
            from tools.voice_mode_transcript import voice_stop_hint
            stop_hint = voice_stop_hint()
    else:
        owner, _epoch, _transport, _capture_id = _voice_scope_snapshot()
        authority_sid, authority_transport = _voice_authority_snapshot()
        caller = _caller_transport()
        sid = str(params.get("session_id") or owner or authority_sid or "__local_voice__")
        epoch = _claim_voice_authority(
            sid,
            caller,
            allow_unclaimed=authority_transport is None,
        )
        if epoch is None:
            return _err(rid, 4016, "voice capture is owned by another session")
        owner, epoch, transport, capture_id = _voice_scope_snapshot()

        def finish_off() -> None:
            _voice_toggle_off_cleanup()
            _clear_voice_authority(session_id=sid, transport=caller)
            _resume_voice_wake(
                expected_transport=transport,
                expected_epoch=epoch,
            )

        released, error = _release_voice_lease(
            session_id=owner, lease_epoch=epoch, transport=transport, stop_loop=True,
            before_expose=finish_off)
        if not released:
            from tools.voice_input import VoiceInputReleaseError
            _voice_terminal_error_epochs.add(epoch)
            _voice_emit(
                "voice.input_error",
                _input_error_payload(VoiceInputReleaseError(error), released=False),
                session_id=owner, lease_epoch=epoch, transport=transport,
                capture_id=capture_id)
            return _err(rid, 5025, error)
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


def _running_smartbox_voice_session(session_id: str, transport) -> bool:
    """True only for the live SmartBox turn containing this profile's caller."""
    with _sessions_lock:
        record = _sessions.get(session_id)
        return bool(
            record
            and record.get("running")
            and record.get("source") == "smartbox_voice"
            and _live_profile_matches(record, _current_voice_profile_home())
            and _session_transport_contains(record, transport)
        )


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
    with contextlib.suppress(Exception):
        from tools.voice_mode import stop_thinking_sound
        stop_thinking_sound()
    with contextlib.suppress(Exception):
        from tools.voice_mode import stop_playback
        stop_playback()


def _voice_toggle_tts(rid, params: dict) -> dict:
    caller = _caller_transport()
    profile_home = _current_voice_profile_home()
    with _voice_lifecycle_lock:
        if not _voice_mode_enabled():
            return _err(rid, 4014, "enable voice mode first: /voice on")
        with _voice_sid_lock:
            authority_sid = _voice_authority_sid
            authority_transport = _voice_authority_transport
            authority_profile_home = _voice_authority_profile_home
            owner = _voice_lease_owner_sid
            owner_transport = _voice_lease_owner_transport
            owner_profile_home = _voice_lease_profile_home
            releasing = _voice_lease_releasing
            sid = str(params.get("session_id") or authority_sid or owner or "")
            physical_owned = owner_transport is None or (
                not releasing
                and sid == owner
                and caller is owner_transport
                and profile_home == owner_profile_home
            )
            authority_owned = authority_transport is None or (
                caller is authority_transport
                and profile_home == authority_profile_home
                and (sid == authority_sid or authority_sid == "__local_voice__")
            )
        if not physical_owned or not authority_owned:
            return _err(rid, 4016, "voice output is owned by another session")
        current = _voice_tts_enabled()
        desired = not current if params.get("enabled") is None else params["enabled"]
        if desired != current:
            _set_voice_tts(desired)
        return _ok(rid, _voice_status_payload())


def _voice_toggle_reattach(rid, params: dict) -> dict:
    """Re-arm a resumed SmartBox turn only after the client adopted ``toggle on``'s epoch."""
    sid = str(params.get("session_id") or "")
    transport = _caller_transport()
    if not sid or not _voice_mode_enabled():
        return _err(rid, 4016, "voice turn is not attachable")
    if not _running_smartbox_voice_session(sid, transport):
        return _err(rid, 4016, "voice turn is not attachable")
    owner, _epoch, owner_transport, _capture_id = _voice_scope_snapshot()
    if (
        owner != sid
        or owner_transport is not transport
        or not _voice_control_scope_accepts(sid, transport)
    ):
        return _err(rid, 4016, "voice turn is not attachable")
    phase = (
        "full_duplex"
        if _voice_cfg_dict().get("barge_in", True)
        else "output_drain"
    )
    armed = (
        _arm_full_duplex_listener(sid, transport)
        if phase == "full_duplex"
        else _arm_output_drain_monitor(sid, transport)
    )
    if not armed:
        return _err(rid, 4016, "voice input is busy in another session")
    return _ok(
        rid,
        _voice_status_payload(reattached=True, phase=phase),
    )


_VOICE_TOGGLE_ACTIONS = {
    "status": _voice_toggle_status, "on": _voice_toggle_mode, "off": _voice_toggle_mode,
    "tts": _voice_toggle_tts, "reattach": _voice_toggle_reattach}


@method("voice.toggle")
@_profile_scoped
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
        session_id=sid, lease_epoch=epoch, transport=transport, stop_loop=True,
        before_expose=lambda: _resume_voice_wake(
            expected_transport=transport, expected_epoch=epoch))
    _vr_emit("voice.input_error", _input_error_payload(error, released=released), scope)
    if not released and str(getattr(error, "stage", "")) != "close":
        from tools.voice_input import VoiceInputReleaseError
        _vr_emit(
            "voice.input_error",
            _input_error_payload(VoiceInputReleaseError(release_error), released=False),
            scope)


def _vr_on_status(state, scope, *, had_transcript: bool = False) -> None:
    global _voice_pending_idle_scope, _voice_release_tail_epoch
    sid, epoch, transport, capture_id = scope
    if state == "idle":
        outcome = "complete" if had_transcript else "no_speech"
        with _voice_sid_lock:
            releasing = epoch == _voice_lease_epoch and _voice_lease_releasing
            if releasing:
                _voice_pending_idle_scope = (*scope, outcome)
                return
        if epoch in _voice_terminal_error_epochs:
            return
        released, error = _release_voice_lease(
            session_id=sid, lease_epoch=epoch, transport=transport, stop_loop=False,
            before_expose=lambda: _resume_voice_wake(
                expected_transport=transport, expected_epoch=epoch))
        if not released:
            from tools.voice_input import VoiceInputReleaseError
            _voice_terminal_error_epochs.add(epoch)
            _vr_emit(
                "voice.input_error",
                _input_error_payload(VoiceInputReleaseError(error), released=False), scope)
            return
        _voice_emit(
            "voice.status",
            {"state": state, "outcome": outcome, "released": True,
             "release_complete": True},
            session_id=sid, lease_epoch=epoch, transport=transport,
            capture_id=capture_id)
        with _voice_sid_lock:
            if _voice_release_tail_epoch == epoch:
                _voice_release_tail_epoch = None
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
    sid, _epoch, transport, _capture_id = scope
    if _end_session_voice_chat(
        sid, stop_loop=True, stop_tts=True, transport=transport,
        lease_epoch=_epoch,
    ):
        _vr_emit("voice.transcript", {"stop_phrase": True, "text": text}, scope)


def _vr_on_silent_limit(scope) -> None:
    sid, _epoch, transport, _capture_id = scope
    if _end_session_voice_chat(
        sid, stop_loop=True, stop_tts=False, transport=transport,
        lease_epoch=_epoch,
    ):
        _vr_emit("voice.transcript", {"no_speech_limit": True}, scope)


def _handoff_unleased_voice_capture(
    session_id: str, lease_epoch: int, transport,
) -> bool:
    """Close a legacy reader, then narrow its temporary release-all fence."""
    global _voice_lease_release_all
    from hermes_cli.voice import stop_continuous
    from tools.voice_input import VoiceInputReleaseError, release_voice_input

    stop_continuous()
    release_voice_input(timeout=_voice_release_timeout(), exclusive_only=False)
    if _capture_live():
        raise VoiceInputReleaseError("unleased voice capture did not stop")
    with _voice_sid_lock:
        current = (
            _voice_lease_owner_sid,
            _voice_lease_epoch,
            _voice_lease_owner_transport,
        ) == (session_id, lease_epoch, transport) and not _voice_lease_releasing
        if current:
            # Future closes belong only to the scoped capture and must preserve
            # unrelated shared PortAudio streams.
            _voice_lease_release_all = False
        return current


def _wake_owned_by_other(transport) -> bool:
    owner, _surface = _wake_owner_snapshot()
    return owner is not None and owner is not transport


def _stop_voice_recording(rid, sid: str, transport) -> dict:
    """Fence and physically stop one exact push-to-talk capture."""
    global _voice_event_sid, _voice_event_transport
    owner, _epoch, _owner_transport, _capture_id = _voice_scope_snapshot()
    had_owner = bool(owner)
    _authority_sid, authority_transport = _voice_authority_snapshot()
    capture_state = _capture_active_state()
    if not had_owner and capture_state is False:
        if not _voice_authority_accepts(sid, transport, allow_unclaimed=True):
            return _err(rid, 4016, "voice capture is owned by another session")
        return _ok(
            rid,
            _voice_result_scope(
                session_id="",
                lease_epoch=_epoch,
                status="stopped",
                released=True,
                release_complete=True,
            ),
        )
    temporary_authority = authority_transport is None
    epoch = _claim_voice_authority(
        sid,
        transport,
        allow_unclaimed=authority_transport is None,
        release_all=not had_owner and capture_state is not False,
    )
    if epoch is None:
        return _err(rid, 4016, "voice capture is owned by another session")
    owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
    if not had_owner:
        with _voice_sid_lock:
            _voice_event_sid = sid or _voice_event_sid
            _voice_event_transport = transport
    temporary_cleanup = (
        lambda *_args: _clear_voice_authority(
            session_id=owner, transport=owner_transport
        )
    ) if temporary_authority else None

    def finish_stop() -> None:
        if temporary_cleanup is not None:
            temporary_cleanup()
        _resume_voice_wake(
            expected_transport=owner_transport,
            expected_epoch=epoch,
        )

    _mark_voice_release_tail(epoch)
    released, error = _release_voice_lease(
        session_id=owner,
        lease_epoch=epoch,
        transport=owner_transport,
        stop_loop=True,
        force_transcribe=True,
        before_expose=finish_stop,
        on_failure=temporary_cleanup,
    )
    if not released:
        from tools.voice_input import VoiceInputReleaseError

        _voice_terminal_error_epochs.add(epoch)
        _voice_emit(
            "voice.input_error",
            _input_error_payload(VoiceInputReleaseError(error), released=False),
            session_id=owner,
            lease_epoch=epoch,
            transport=owner_transport,
            capture_id=capture_id,
        )
        return _err(rid, 5025, error)
    return _ok(
        rid,
        _voice_result_scope(
            session_id=owner,
            lease_epoch=epoch,
            capture_id=capture_id,
            status="stopped",
            released=True,
            release_complete=True,
        ),
    )


@method("voice.record")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """VAD-bounded push-to-talk. ``start`` emits ``voice.transcript`` when silence stops the
    capture; ``stop`` forces transcription. Three silent captures emit ``no_speech_limit``."""
    action = params.get("action", "start")
    wake_paused = False
    if action not in {"start", "stop"}:
        return _err(rid, 4019, f"unknown voice action: {action}")
    transport = _caller_transport()
    if _wake_owned_by_other(transport):
        return _ok(rid, {"status": "busy", "reason": "wake_owned"})
    try:
        global _voice_event_sid, _voice_event_transport, _voice_wake_owner, _voice_capture_sid
        sid = str(params.get("session_id") or "")
        if action == "start" and not _session_voice_enabled(sid):
            return _err(rid, 4015, "voice mode is off — enable with /voice on")
        owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        if action == "stop":
            return _stop_voice_recording(rid, sid, transport)

        capture_live = _capture_live()
        authority_sid, authority_transport = _voice_authority_snapshot()
        same_transport_handoff = (
            not capture_live
            and authority_transport is transport
            and authority_sid not in {"", sid, "__local_voice__"}
        )
        if not same_transport_handoff and not _voice_authority_accepts(
            sid, transport, allow_unclaimed=True
        ):
            return _err(rid, 4016, "voice capture is owned by another session")
        if owner and not capture_live:
            with _fd_listener_lock:
                full_duplex_active = _fd_listener_active
            placeholder_handoff = (
                owner == "__local_voice__" and owner_transport is transport
            )
            same_scope = owner == sid and owner_transport is transport
            if not full_duplex_active and not placeholder_handoff and not same_scope:
                release_failures = []
                released, release_error = _release_voice_lease(
                    session_id=owner, lease_epoch=epoch, transport=owner_transport,
                    stop_loop=False,
                    before_expose=lambda: _resume_voice_wake(
                        expected_transport=owner_transport,
                        expected_epoch=epoch,
                    ),
                    on_failure=release_failures.append)
                if not released:
                    if not release_failures:
                        return _err(rid, 4016, release_error)
                    from tools.voice_input import VoiceInputReleaseError
                    _voice_terminal_error_epochs.add(epoch)
                    failure = release_failures[-1]
                    _voice_emit(
                        "voice.input_error",
                        _input_error_payload(
                            failure if isinstance(failure, VoiceInputReleaseError)
                            else VoiceInputReleaseError(str(failure)),
                            released=False,
                        ),
                        session_id=owner,
                        lease_epoch=epoch,
                        transport=owner_transport,
                        capture_id=capture_id,
                    )
                    return _err(rid, 5025, release_error)
                owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        unleased_capture = owner_transport is None and capture_live
        requested_capture = capture_id or uuid.uuid4().hex
        with _voice_lifecycle_lock:
            predecessor_sid, predecessor_transport = _voice_authority_snapshot()
            _, predecessor_epoch, _, _ = _voice_scope_snapshot()
            previous_epoch = (
                predecessor_epoch
                if predecessor_sid == sid and predecessor_transport is transport
                else None
            )
            epoch = _claim_voice_authority(
                sid,
                transport,
                capture_id=requested_capture,
                allow_unclaimed=True,
                allow_same_transport_handoff=same_transport_handoff,
                release_all=unleased_capture,
            )
            if epoch is None:
                return _err(rid, 4016, "voice capture is busy in another session")
            owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
            record_predecessor_epoch = (
                previous_epoch
                if previous_epoch is not None and previous_epoch > 0 and previous_epoch != epoch
                else None
            )
            _emit_voice_lease_changed(
                owner, epoch, owner_transport, record_predecessor_epoch, "ordinary_record"
            )
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
        transcript_ready = threading.Event()

        def live(fn):
            return _voice_capture_callback(
                fn, session_id=owner, lease_epoch=epoch, transport=owner_transport)

        def release_and_publish_transcript(text: str) -> None:
            current_owner, current_epoch, current_transport, _ = _voice_scope_snapshot()
            if (
                current_epoch == epoch
                and current_transport is None
                and not current_owner
            ):
                # A forced stop already released this captured epoch.  Its
                # recorder is still entitled to one final transcript tail.
                _vr_emit("voice.transcript", {"text": text}, scope)
                return
            _mark_voice_release_tail(epoch)

            def publish_after_release() -> None:
                _resume_voice_wake(
                    expected_transport=owner_transport,
                    expected_epoch=epoch,
                )
                _vr_emit("voice.transcript", {"text": text}, scope)

            released, release_error = _release_voice_lease(
                session_id=owner,
                lease_epoch=epoch,
                transport=owner_transport,
                stop_loop=False,
                before_expose=publish_after_release,
            )
            if not released:
                from tools.voice_input import VoiceInputReleaseError

                _voice_terminal_error_epochs.add(epoch)
                _vr_emit(
                    "voice.input_error",
                    _input_error_payload(
                        VoiceInputReleaseError(release_error), released=False
                    ),
                    scope,
                )

        def on_transcript(text: str) -> None:
            transcript_ready.set()
            # A forced-stop tail may arrive on its background worker while
            # the stop RPC still owns the physical-release fence.  Wait for
            # that exact release to expose its terminal scope before deciding
            # whether this callback must release or only publish the tail.
            with _voice_lifecycle_lock:
                release_and_publish_transcript(text)

        with _voice_input_start_barrier(owner, epoch, owner_transport) as can_start:
            if not can_start:
                return _err(rid, 4016, "voice input release is in progress")
            if unleased_capture and not _handoff_unleased_voice_capture(
                owner, epoch, owner_transport
            ):
                return _err(rid, 4016, "voice input release is in progress")
            # Hand the mic to STT if the wake detector holds it; a terminal capture event resumes it.
            with contextlib.suppress(Exception):
                from tools.wake_word import pause_listening
                wake_paused = pause_listening(owner=transport)
            if wake_paused:
                with _voice_sid_lock:
                    _voice_wake_owner = transport
            started = start_continuous(
                on_transcript=live(on_transcript),
                on_status=live(lambda state: _vr_on_status(
                    state, scope, had_transcript=transcript_ready.is_set())),
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
                transport=owner_transport, stop_loop=True,
                before_expose=(lambda: _resume_voice_wake(
                    expected_transport=owner_transport,
                    expected_epoch=epoch,
                )) if wake_paused else None)
            if not released:
                from tools.voice_input import VoiceInputReleaseError
                error = VoiceInputReleaseError(release_error)
                _voice_emit(
                    "voice.input_error", _input_error_payload(error, released=False),
                    session_id=owner, lease_epoch=epoch, transport=owner_transport,
                    capture_id=capture_id)
                return _err(rid, 5025, release_error)
        result_extra = (
            {"previous_lease_epoch": record_predecessor_epoch}
            if started is not False and record_predecessor_epoch is not None
            else {}
        )
        return _ok(rid, _voice_result_scope(
            session_id=owner, lease_epoch=epoch, capture_id=capture_id,
            status="busy" if started is False else "recording",
            released=started is False, release_complete=started is False,
            **result_extra))
    except Exception as e:
        owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
        released = False
        if owner and owner_transport is transport:
            released, release_error = _release_voice_lease(
                session_id=owner, lease_epoch=epoch, transport=owner_transport, stop_loop=True,
                before_expose=(lambda: _resume_voice_wake(
                    expected_transport=owner_transport,
                    expected_epoch=epoch,
                )) if wake_paused else None)
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
        if isinstance(e, ImportError):
            return _err(rid, 5025, "voice module not available — install audio dependencies")
        return _err(rid, 5025, str(e))


@method("voice.tts")
@_profile_scoped
def _(rid, params: dict) -> dict:
    text = params.get("text", "")
    if not text:
        return _err(rid, 4020, "text required")
    owner, epoch, owner_transport, capture_id = _voice_scope_snapshot()
    authority_sid, authority_transport = _voice_authority_snapshot()
    sid = str(params.get("session_id") or owner or authority_sid or "")
    caller = _caller_transport()
    profile_home = _current_voice_profile_home()
    with _voice_sid_lock:
        owner_profile_home = _voice_lease_profile_home
    require_voice_scope = bool(
        owner_transport is not None
        or authority_transport is not None
        or _voice_mode_enabled()
    )
    if authority_transport is not None and not _voice_authority_accepts(
        sid, caller, allow_unclaimed=False
    ):
        return _err(rid, 4016, "voice output is owned by another session")
    if owner and (
        sid != owner
        or caller is not owner_transport
        or profile_home != owner_profile_home
    ):
        return _err(rid, 4016, "voice output is owned by another session")
    try:
        import hermes_cli.voice
    except Exception as e:
        return _err(rid, 5026, "voice module not available" if isinstance(e, ImportError) else str(e))
    if not owner and _voice_mode_enabled() and _voice_cfg_dict().get("barge_in", True):
        with _voice_lifecycle_lock:
            scope = _full_duplex_scope(sid, caller)
            if scope is None:
                return _err(rid, 4016, "voice input is busy in another session")
            owner, epoch, owner_transport, previous_epoch = scope
            _emit_voice_lease_changed(
                owner, epoch, owner_transport, previous_epoch, "full_duplex"
            )
    _start_voice_thread(
        _speak_text_with_barge,
        name="voice-tts-rpc",
        args=(text, owner or sid, owner_transport or caller, require_voice_scope),
    )
    return _ok(rid, _voice_result_scope(
        session_id=owner or sid, lease_epoch=epoch if owner else 0,
        capture_id=capture_id, status="speaking"))


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
