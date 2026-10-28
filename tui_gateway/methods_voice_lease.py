"""Process-global voice lease and authority lifecycle helpers.

Kept separate from the JSON-RPC facade so the physical-input fence remains
small enough to audit. Functions are rebound onto ``tui_gateway.server``.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Optional

from .method_ctx import bind_module
from .transport import Transport

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
_voice_lease_profile_home: str | None = None
_voice_lease_release_all = False
_voice_release_tail_epoch: int | None = None
_voice_pending_idle_scope: tuple[str, int, Optional[Transport], str | None] | None = None
_voice_pending_disconnect_scope: tuple[str, int, Optional[Transport]] | None = None
_voice_authority_sid: str = ""
_voice_authority_transport: Optional[Transport] = None
_voice_authority_profile_home: str | None = None
_voice_terminal_error_epochs: set[int] = set()
_voice_release_retry_active = False
_voice_lifecycle_lock = threading.RLock()
_voice_tts_lease_lock = threading.Lock()
_voice_tts_lease_generation = 0
_voice_tts_lease_desired: dict[str, tuple[int, bool]] = {}
_VOICE_RELEASE_RETRY_SECONDS = 1.0


def _caller_transport():
    return current_transport() or _stdio_transport


def _current_voice_profile_home() -> str | None:
    """Captured owner profile (``None`` is the launch profile)."""
    from hermes_constants import get_hermes_home
    from pathlib import Path

    home = Path(get_hermes_home()).resolve()
    return None if home == Path(_hermes_home).resolve() else str(home)


def _start_voice_thread(target, *, name: str, args: tuple = ()):
    """Start a daemon voice worker without dropping the caller's profile ContextVars."""
    from agent.memory_provider import spawn_context_thread

    thread = spawn_context_thread(target, name=name, args=args)
    thread.start()
    return thread


def _voice_scope_snapshot() -> tuple[str, int, Optional[Transport], str | None]:
    with _voice_sid_lock:
        return (
            _voice_lease_owner_sid,
            _voice_lease_epoch,
            _voice_lease_owner_transport,
            _voice_lease_capture_id,
        )


def _voice_authority_snapshot() -> tuple[str, Optional[Transport]]:
    with _voice_sid_lock:
        return _voice_authority_sid, _voice_authority_transport


def _voice_authority_accepts(
    session_id: str, transport, *, allow_unclaimed: bool,
) -> bool:
    profile_home = _current_voice_profile_home()
    with _voice_sid_lock:
        return _voice_authority_locked_accepts(
            session_id,
            transport,
            profile_home=profile_home,
            allow_unclaimed=allow_unclaimed,
        )


def _voice_authority_locked_accepts(
    session_id: str,
    transport,
    *,
    profile_home: str | None,
    allow_unclaimed: bool,
    allow_same_transport_handoff: bool = False,
) -> bool:
    if _voice_authority_transport is None:
        return allow_unclaimed
    if profile_home != _voice_authority_profile_home:
        return False
    return transport is _voice_authority_transport and (
        session_id == _voice_authority_sid
        or _voice_authority_sid == "__local_voice__"
        or allow_same_transport_handoff
    )


def _voice_control_scope_accepts(session_id: str, transport) -> bool:
    """Accept an unscoped native call or this exact SID/transport/profile owner."""
    profile_home = _current_voice_profile_home()
    with _voice_sid_lock:
        if _voice_lease_releasing:
            return False
        if _voice_authority_transport is not None:
            authority_matches = (
                transport is _voice_authority_transport
                and profile_home == _voice_authority_profile_home
                and _voice_authority_sid in {session_id, "__local_voice__"}
            )
            if not authority_matches or _voice_lease_owner_transport is None:
                return authority_matches
            return (
                transport is _voice_lease_owner_transport
                and profile_home == _voice_lease_profile_home
                and _voice_lease_owner_sid in {session_id, "__local_voice__"}
            )
        if _voice_lease_owner_transport is not None:
            return (
                transport is _voice_lease_owner_transport
                and profile_home == _voice_lease_profile_home
                and _voice_lease_owner_sid in {session_id, "__local_voice__"}
            )
        return True


def _install_voice_authority_locked(
    session_id: str,
    transport,
    *,
    profile_home: str | None,
    allow_unclaimed: bool,
    allow_same_transport_handoff: bool = False,
) -> bool:
    global _voice_authority_sid, _voice_authority_transport, _voice_authority_profile_home
    if not _voice_authority_locked_accepts(
        session_id,
        transport,
        profile_home=profile_home,
        allow_unclaimed=allow_unclaimed,
        allow_same_transport_handoff=allow_same_transport_handoff,
    ):
        return False
    if _voice_authority_transport is None or session_id != _voice_authority_sid:
        _voice_authority_profile_home = profile_home
    _voice_authority_sid = session_id
    _voice_authority_transport = transport
    return True


def _install_voice_authority(
    session_id: str,
    transport,
    *,
    allow_unclaimed: bool,
    allow_same_transport_handoff: bool = False,
) -> bool:
    profile_home = _current_voice_profile_home()
    with _voice_sid_lock:
        return _install_voice_authority_locked(
            session_id,
            transport,
            profile_home=profile_home,
            allow_unclaimed=allow_unclaimed,
            allow_same_transport_handoff=allow_same_transport_handoff,
        )


def _clear_voice_authority(*, session_id: str | None = None, transport=None) -> bool:
    global _voice_authority_sid, _voice_authority_transport, _voice_authority_profile_home
    with _voice_sid_lock:
        if _voice_authority_transport is None:
            return False
        if session_id is not None and session_id != _voice_authority_sid:
            return False
        if transport is not None and transport is not _voice_authority_transport:
            return False
        _voice_authority_sid = ""
        _voice_authority_transport = None
        _voice_authority_profile_home = None
        return True


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


def _acquire_voice_lease_locked(
    session_id: str, transport, *, capture_id: str | None = None,
    profile_home: str | None = None, release_all: bool = False,
) -> int | None:
    global _voice_lease_owner_sid, _voice_lease_owner_transport, _voice_lease_epoch
    global _voice_lease_releasing, _voice_lease_capture_id, _voice_lease_profile_home
    global _voice_lease_release_all
    global _voice_release_tail_epoch, _voice_pending_idle_scope
    if _voice_lease_owner_transport is not None:
        if profile_home != _voice_lease_profile_home:
            return None
        if (not _voice_lease_releasing and session_id == _voice_lease_owner_sid
                and transport is _voice_lease_owner_transport):
            if capture_id and not _voice_lease_capture_id:
                _voice_lease_capture_id = capture_id
            _voice_lease_release_all = _voice_lease_release_all or release_all
            return _voice_lease_epoch
        if (
            not _voice_lease_releasing
            and _voice_lease_owner_sid == "__local_voice__"
            and transport is _voice_lease_owner_transport
            and session_id
            and session_id != "__local_voice__"
        ):
            _voice_lease_owner_sid = session_id
            _voice_lease_profile_home = profile_home
            _voice_lease_release_all = _voice_lease_release_all or release_all
            if capture_id:
                _voice_lease_capture_id = capture_id
            return _voice_lease_epoch
        return None
    # A successful retry clears the old physical owner before its worker has
    # finished wake cleanup and dropped the process-wide retry guard.  Do not
    # expose that narrow window to a successor: if its first close failed, the
    # still-running old worker would otherwise suppress its reaper.
    if _voice_lease_releasing or _voice_release_retry_active:
        return None
    _voice_lease_epoch += 1
    _voice_lease_owner_sid = session_id
    _voice_lease_owner_transport = transport
    _voice_lease_capture_id = capture_id
    _voice_lease_profile_home = profile_home
    _voice_lease_release_all = release_all
    _voice_release_tail_epoch = None
    _voice_pending_idle_scope = None
    _voice_terminal_error_epochs.discard(_voice_lease_epoch)
    return _voice_lease_epoch


def _acquire_voice_lease(
    session_id: str, transport, *, capture_id: str | None = None,
    release_all: bool = False,
) -> int | None:
    profile_home = _current_voice_profile_home()
    with _voice_sid_lock:
        return _acquire_voice_lease_locked(
            session_id, transport, capture_id=capture_id, profile_home=profile_home,
            release_all=release_all,
        )


def _claim_voice_authority(
    session_id: str,
    transport,
    *,
    capture_id: str | None = None,
    allow_unclaimed: bool,
    allow_same_transport_handoff: bool = False,
    release_all: bool = False,
    expected_epoch: int | None = None,
) -> int | None:
    """Atomically claim the logical authority and physical lease for one live transport."""
    profile_home = _current_voice_profile_home()
    with _voice_sid_lock:
        if bool(getattr(transport, "_closed", False)):
            return None
        if expected_epoch is not None and not (
            not _voice_lease_releasing
            and _voice_lease_epoch == expected_epoch
            and _voice_lease_owner_sid == session_id
            and _voice_lease_owner_transport is transport
        ):
            return None
        if not _voice_authority_locked_accepts(
            session_id,
            transport,
            profile_home=profile_home,
            allow_unclaimed=allow_unclaimed,
            allow_same_transport_handoff=allow_same_transport_handoff,
        ):
            return None
        epoch = _acquire_voice_lease_locked(
            session_id,
            transport,
            capture_id=capture_id,
            profile_home=profile_home,
            release_all=release_all,
        )
        if epoch is None:
            return None
        if not _install_voice_authority_locked(
            session_id,
            transport,
            profile_home=profile_home,
            allow_unclaimed=allow_unclaimed,
            allow_same_transport_handoff=allow_same_transport_handoff,
        ):
            return None
        return epoch


def _claim_voice_transport_release(
    transport, *, expected_session_id: str | None = None, expected_epoch: int | None = None,
):
    """Atomically turn this transport's idle authority into a physical release fence."""
    global _voice_lease_releasing, _voice_pending_disconnect_scope
    with _voice_sid_lock:
        if _voice_lease_owner_transport is transport:
            if expected_session_id is not None and _voice_lease_owner_sid != expected_session_id:
                return None
            if expected_epoch is not None and _voice_lease_epoch != expected_epoch:
                return None
            already_releasing = _voice_lease_releasing
            _voice_lease_releasing = True
            _voice_pending_disconnect_scope = (
                _voice_lease_owner_sid, _voice_lease_epoch, transport
            )
            return (
                _voice_lease_owner_sid,
                _voice_lease_epoch,
                transport,
                _voice_lease_capture_id,
                _voice_lease_profile_home,
                already_releasing,
                False,
            )
        if _voice_authority_transport is not transport:
            return None
        if expected_session_id is not None and _voice_authority_sid != expected_session_id:
            return None
        if expected_epoch is not None and _voice_lease_epoch != expected_epoch:
            return None
        if _voice_release_retry_active:
            # The prior physical owner is already gone, but its retry worker
            # still owns the process reaper slot until wake cleanup returns.
            # Disconnect may retire this exact logical authority without
            # synthesizing another physical lease that could fail unreaped.
            return (
                _voice_authority_sid,
                _voice_lease_epoch,
                transport,
                None,
                _voice_authority_profile_home,
                False,
                True,
            )
        epoch = _acquire_voice_lease_locked(
            _voice_authority_sid,
            transport,
            profile_home=_voice_authority_profile_home,
        )
        if epoch is None:
            return None
        _voice_lease_releasing = True
        _voice_pending_disconnect_scope = (
            _voice_lease_owner_sid, epoch, transport
        )
        return (
            _voice_lease_owner_sid,
            epoch,
            _voice_lease_owner_transport,
            _voice_lease_capture_id,
            _voice_lease_profile_home,
            False,
            False,
        )


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
    on_failure=None,
) -> tuple[bool, str]:
    """Release under the profile captured with the physical owner, including off-turn teardown."""
    with _voice_sid_lock:
        if (_voice_lease_owner_transport is None and lease_epoch == _voice_lease_epoch):
            return True, ""
        if not (session_id == _voice_lease_owner_sid and lease_epoch == _voice_lease_epoch
                and transport is _voice_lease_owner_transport):
            return False, "voice input lease is owned by another session"
        profile_home = _voice_lease_profile_home
    try:
        with _session_profile_runtime_scope({"profile_home": profile_home}):
            return _release_voice_lease_scoped(
                session_id=session_id,
                lease_epoch=lease_epoch,
                transport=transport,
                stop_loop=stop_loop,
                force_transcribe=force_transcribe,
                _retry=_retry,
                before_expose=before_expose,
                on_failure=on_failure,
            )
    except Exception as exc:  # health: allow BLE001 -- profile/release boundary: preserve the cleanup fence and arm its reaper
        if on_failure is not None:
            with contextlib.suppress(Exception):
                on_failure(exc)
        _schedule_voice_release_retry(session_id, lease_epoch, transport)
        return False, str(exc)


def _consume_pending_voice_disconnect(session_id: str, lease_epoch: int, transport) -> bool:
    """Run dead-transport cleanup before the claimed physical scope is exposed."""
    global _voice_pending_disconnect_scope
    expected = (session_id, lease_epoch, transport)
    with _voice_sid_lock:
        if _voice_pending_disconnect_scope != expected:
            return False
        owns_authority = _voice_authority_transport is transport
    if owns_authority or _voice_mode_enabled() or _voice_tts_enabled():
        _voice_toggle_off_cleanup()
        _clear_voice_authority(transport=transport)
    with _voice_sid_lock:
        if _voice_pending_disconnect_scope == expected:
            _voice_pending_disconnect_scope = None
    return True


def _release_voice_lease_scoped(
    *, session_id: str, lease_epoch: int, transport, stop_loop: bool,
    force_transcribe: bool = False, _retry: bool = False, before_expose=None,
    on_failure=None,
) -> tuple[bool, str]:
    """Invalidate/release one captured owner only after physical input closes."""
    global _voice_lease_owner_sid, _voice_lease_owner_transport, _voice_lease_releasing
    global _voice_lease_capture_id, _voice_lease_profile_home
    global _voice_lease_release_all
    with _voice_sid_lock:
        if (_voice_lease_owner_transport is None and lease_epoch == _voice_lease_epoch):
            return True, ""
        if not (session_id == _voice_lease_owner_sid and lease_epoch == _voice_lease_epoch
                and transport is _voice_lease_owner_transport):
            return False, "voice input lease is owned by another session"
        if _voice_lease_releasing and not _retry:
            return False, "voice input release is still in progress"
        _voice_lease_releasing = True
        release_all = _voice_lease_release_all
    try:
        with _voice_lifecycle_lock:
            if stop_loop:
                from hermes_cli.voice import stop_continuous
                stop_continuous(force_transcribe=force_transcribe)
            from tools.voice_input import release_voice_input
            release_voice_input(
                timeout=_voice_release_timeout(), exclusive_only=not release_all
            )
            if before_expose is not None:
                before_expose()
            expected_disconnect = (session_id, lease_epoch, transport)
            while True:
                _consume_pending_voice_disconnect(session_id, lease_epoch, transport)
                with _voice_sid_lock:
                    if not (
                        session_id == _voice_lease_owner_sid
                        and lease_epoch == _voice_lease_epoch
                        and transport is _voice_lease_owner_transport
                    ):
                        _voice_lease_releasing = False
                        return False, "voice input lease changed during release"
                    if _voice_pending_disconnect_scope == expected_disconnect:
                        continue
                    _voice_lease_owner_sid = ""
                    _voice_lease_owner_transport = None
                    _voice_lease_capture_id = None
                    _voice_lease_profile_home = None
                    _voice_lease_release_all = False
                    _voice_lease_releasing = False
                    break
    except Exception as exc:  # health: allow BLE001 -- physical close boundary: every failure remains fenced and is retried
        # Keep the cleanup fence and captured transport installed: even the
        # same client must not reopen the mic while the failed close is reaped.
        if on_failure is not None:
            with contextlib.suppress(Exception):
                on_failure(exc)
        _schedule_voice_release_retry(session_id, lease_epoch, transport)
        return False, str(exc)
    _flush_pending_voice_idle(session_id, lease_epoch, transport)
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
        profile_home = _voice_lease_profile_home

    def retry():
        global _voice_release_retry_active
        try:
            while _voice_cleanup_scope_matches(session_id, lease_epoch, transport):
                time.sleep(_VOICE_RELEASE_RETRY_SECONDS)
                released, _error = _release_voice_lease(
                    session_id=session_id, lease_epoch=lease_epoch, transport=transport,
                    stop_loop=True, _retry=True)
                if released:
                    with _session_profile_runtime_scope({"profile_home": profile_home}):
                        _resume_voice_wake()
                    return
        finally:
            with _voice_sid_lock:
                _voice_release_retry_active = False
    _start_voice_thread(retry, name="voice-release-retry")


def _release_voice_for_transport(
    transport, *, expected_session_id: str | None = None, expected_epoch: int | None = None,
) -> bool:
    scope = _claim_voice_transport_release(
        transport,
        expected_session_id=expected_session_id,
        expected_epoch=expected_epoch,
    )
    if scope is None:
        return False
    (
        session_id,
        lease_epoch,
        _owner,
        _capture_id,
        profile_home,
        already_releasing,
        logical_only,
    ) = scope

    def finish_disconnect() -> None:
        _voice_toggle_off_cleanup()
        _clear_voice_authority(transport=transport)

    with _session_profile_runtime_scope({"profile_home": profile_home}):
        if logical_only:
            finish_disconnect()
            return True
        if already_releasing:
            finish_disconnect()
            return False
        released, _error = _release_voice_lease(
            session_id=session_id, lease_epoch=lease_epoch, transport=transport, stop_loop=True,
            _retry=True)
        if released:
            return True
        finish_disconnect()
        _schedule_voice_release_retry(session_id, lease_epoch, transport)
        return False


def _resume_voice_wake() -> None:
    global _voice_wake_owner
    with _voice_sid_lock:
        owner, _voice_wake_owner = _voice_wake_owner, None
    if owner is not None:
        _wake_resume_if_owner(owner)


def register(server) -> None:
    bind_module(globals(), server, skip=("Optional", "Transport"))
