"""One fenced, session-scoped lease owns the server microphone."""

from __future__ import annotations

import sys
import threading
import types

import pytest

from tests.waiting import wait_until
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


class _Sink:
    def __init__(self):
        self.frames = []
        self._closed = False

    def write(self, obj):
        self.frames.append(obj)
        return True

    def close(self):
        self._closed = True


class _Mic:
    def __init__(self):
        self.active = False
        self.callbacks = {}
        self.stop_calls = 0
        self.stop_impl = None
        self.operations = []

    def start_continuous(self, **kwargs):
        self.operations.append("start")
        self.active = True
        self.callbacks = kwargs
        return True

    def stop_continuous(self, **kwargs):
        self.operations.append("stop")
        self.stop_calls += 1
        if self.stop_impl is not None:
            return self.stop_impl(**kwargs)
        self.active = False

    def is_continuous_active(self):
        return self.active

    def set_voice_busy_probe(self, _probe):
        pass


@pytest.fixture
def voice(monkeypatch):
    mic, sink = _Mic(), _Sink()
    monkeypatch.setitem(sys.modules, "hermes_cli.voice", mic)
    monkeypatch.setenv("HERMES_VOICE", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {}})
    monkeypatch.setattr(server, "_session_voice_enabled", lambda _sid: True)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: None)
    monkeypatch.setattr(server, "_stdio_transport", sink)
    monkeypatch.setattr(server, "_current_voice_profile_home", lambda: None)
    monkeypatch.setattr(server, "_voice_event_sid", "")
    monkeypatch.setattr(server, "_voice_event_transport", None)
    monkeypatch.setattr(server, "_voice_capture_sid", "")
    monkeypatch.setattr(server, "_voice_capture_gen", 0)
    for name, value in {
        "_voice_lease_owner_sid": "",
        "_voice_lease_owner_transport": None,
        "_voice_lease_epoch": 0,
        "_voice_lease_releasing": False,
        "_voice_lease_capture_id": None,
        "_voice_lease_profile_home": None,
        "_voice_lease_release_all": False,
        "_voice_release_tail_epoch": None,
        "_voice_pending_idle_scope": None,
        "_voice_pending_disconnect_scope": None,
        "_voice_authority_sid": "",
        "_voice_authority_transport": None,
        "_voice_authority_profile_home": None,
        "_voice_terminal_error_epochs": set(),
        "_voice_release_retry_active": False,
        "_voice_tts_lease_generation": 0,
        "_voice_tts_lease_desired": {},
        "_fd_listener_active": False,
        "_fd_listener_epoch": 0,
        "_output_drain_stop": None,
    }.items():
        monkeypatch.setattr(server, name, value, raising=False)
    from tools import voice_input
    monkeypatch.setattr(voice_input, "release_voice_input", lambda **_kw: True)

    def rpc(transport, method, **params):
        token = bind_transport(transport)
        try:
            return server.handle_request({"id": method, "method": method, "params": params})
        finally:
            reset_transport(token)

    return types.SimpleNamespace(mic=mic, sink=sink, rpc=rpc)


def _events(sink, kind):
    return [
        frame["params"]
        for frame in sink.frames
        if frame.get("params", {}).get("type") == kind
    ]


def _set_fake_playback(monkeypatch, stopped):
    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(stop_playback=lambda: stopped.append(True)),
    )


def test_record_result_and_callbacks_carry_captured_owner_epoch(voice):
    owner = _Sink()
    response = voice.rpc(owner, "voice.record", action="start", session_id="owner")

    result = response["result"]
    assert result["status"] == "recording"
    assert result["owner_session_id"] == "owner"
    assert result["lease_epoch"] > 0
    assert result["capture_id"]

    voice.mic.callbacks["on_partial"]("hel")
    voice.mic.callbacks["on_transcript"]("hello")
    for kind in ("voice.partial", "voice.transcript"):
        payload = _events(owner, kind)[-1]["payload"]
        assert payload["owner_session_id"] == "owner"
        assert payload["lease_epoch"] == result["lease_epoch"]
        assert payload["capture_id"] == result["capture_id"]


def test_record_after_toggle_allocates_capture_id(voice):
    owner = _Sink()
    toggled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")

    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")

    assert toggled["result"].get("capture_id") is None
    assert started["result"]["capture_id"]
    assert server._voice_lease_capture_id == started["result"]["capture_id"]


def test_voice_toggle_on_claims_session_lease_and_off_acks_empty_owner(voice):
    owner = _Sink()

    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    epoch = enabled["result"]["lease_epoch"]

    assert enabled["result"]["owner_session_id"] == "owner"
    assert epoch > 0
    disabled = voice.rpc(owner, "voice.toggle", action="off", session_id="owner")
    assert disabled["result"]["enabled"] is False
    assert disabled["result"]["owner_session_id"] == ""
    assert disabled["result"]["lease_epoch"] == epoch
    assert disabled["result"]["released"] is True
    assert disabled["result"]["release_complete"] is True


def test_local_toggle_lease_promotes_to_first_real_session_on_same_transport(voice):
    owner = _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on")

    started = voice.rpc(
        owner, "voice.record", action="start", session_id="runtime-session"
    )

    assert started["result"]["status"] == "recording"
    assert started["result"]["owner_session_id"] == "runtime-session"
    assert started["result"]["lease_epoch"] == enabled["result"]["lease_epoch"]


def test_idle_voice_authority_blocks_unrelated_turn_from_claiming_lease(voice):
    owner, other = _Sink(), _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    epoch = enabled["result"]["lease_epoch"]
    released, error = server._release_voice_lease(
        session_id="owner", lease_epoch=epoch, transport=owner, stop_loop=False
    )
    assert released is True and error == ""
    assert server._voice_lease_owner_sid == ""

    assert server._full_duplex_scope("other", other) is None
    reacquired = server._full_duplex_scope("owner", owner)
    assert reacquired is not None
    assert reacquired[0] == "owner"
    assert reacquired[1] > epoch


def test_same_session_new_transport_cannot_rebind_live_idle_authority(voice):
    owner, replacement = _Sink(), _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    released, error = server._release_voice_lease(
        session_id="owner",
        lease_epoch=enabled["result"]["lease_epoch"],
        transport=owner,
        stop_loop=False,
    )
    assert released is True and error == ""

    refused = voice.rpc(
        replacement, "voice.toggle", action="on", session_id="owner"
    )

    assert refused["error"]["code"] == 4016
    assert server._voice_authority_transport is owner


def test_old_session_teardown_cannot_release_same_transport_handoff(voice):
    shared = _Sink()
    enabled = voice.rpc(shared, "voice.toggle", action="on", session_id="old")
    old_epoch = enabled["result"]["lease_epoch"]
    released, error = server._release_voice_lease(
        session_id="old", lease_epoch=old_epoch, transport=shared, stop_loop=False
    )
    assert released is True and error == ""
    new_epoch = server._claim_voice_authority(
        "successor",
        shared,
        allow_unclaimed=True,
        allow_same_transport_handoff=True,
    )

    stale = server._release_voice_for_transport(
        shared, expected_session_id="old", expected_epoch=old_epoch
    )

    assert stale is False
    assert new_epoch and new_epoch > old_epoch
    assert server._voice_lease_owner_sid == "successor"
    assert server._voice_authority_sid == "successor"


def test_closed_transport_cannot_claim_voice_authority(voice):
    disconnected = _Sink()
    disconnected.close()

    refused = voice.rpc(
        disconnected, "voice.toggle", action="on", session_id="stale"
    )

    assert refused["error"]["code"] == 4016
    assert server._voice_lease_owner_transport is None
    assert server._voice_authority_transport is None


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("voice.toggle", {"action": "on", "session_id": "owner"}),
        ("voice.record", {"action": "start", "session_id": "owner"}),
    ],
)
def test_lease_and_authority_claim_have_no_visible_split_state(
    voice, monkeypatch, method, params,
):
    windows = []

    class _ObservingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()
            if (
                server._voice_lease_owner_transport is not None
                and server._voice_authority_transport is None
            ):
                windows.append("lease_without_authority")

    monkeypatch.setattr(server, "_voice_sid_lock", _ObservingLock())
    owner = _Sink()

    response = voice.rpc(owner, method, **params)

    assert "error" not in response
    assert windows == []
    assert server._voice_lease_owner_transport is owner
    assert server._voice_authority_transport is owner


def test_disconnected_idle_authority_cannot_be_claimed_by_an_ordinary_turn(voice):
    owner, other = _Sink(), _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    released, error = server._release_voice_lease(
        session_id="owner",
        lease_epoch=enabled["result"]["lease_epoch"],
        transport=owner,
        stop_loop=False,
    )
    assert released is True and error == ""

    assert server._release_voice_for_transport(owner) is True
    assert server._full_duplex_scope("other", other) is None

    explicitly_enabled = voice.rpc(
        other, "voice.toggle", action="on", session_id="other"
    )
    assert explicitly_enabled["result"]["owner_session_id"] == "other"


def test_unrelated_toggle_off_cannot_clear_idle_voice_authority(voice):
    owner, other = _Sink(), _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    released, error = server._release_voice_lease(
        session_id="owner",
        lease_epoch=enabled["result"]["lease_epoch"],
        transport=owner,
        stop_loop=False,
    )
    assert released is True and error == ""

    refused = voice.rpc(other, "voice.toggle", action="off", session_id="other")

    assert refused["error"]["code"] == 4016
    assert server._voice_mode_enabled() is True
    assert server._full_duplex_scope("owner", owner) is not None


def test_unrelated_toggle_tts_cannot_cut_voice_owners_output(voice, monkeypatch):
    owner, other = _Sink(), _Sink()
    voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    toggles = []
    monkeypatch.setattr(server, "_set_voice_tts", lambda on: toggles.append(on))

    refused = voice.rpc(other, "voice.toggle", action="tts", session_id="other")

    assert refused["error"]["code"] == 4016
    assert toggles == []


def test_same_transport_other_session_cannot_toggle_physical_owners_tts(
    voice, monkeypatch
):
    shared = _Sink()
    assert server._acquire_voice_lease("owner", shared)
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    toggles = []
    monkeypatch.setattr(server, "_set_voice_tts", lambda on: toggles.append(on))

    refused = voice.rpc(shared, "voice.toggle", action="tts", session_id="other")

    assert refused["error"]["code"] == 4016
    assert toggles == []


def test_stale_tts_toggle_revalidates_after_disconnect_handoff(voice, monkeypatch):
    owner, successor = _Sink(), _Sink()
    voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    monkeypatch.setenv("HERMES_VOICE_TTS", "0")
    monkeypatch.setattr(server, "_voice_toggle_off_cleanup", lambda: None)
    toggles = []
    monkeypatch.setattr(server, "_set_voice_tts", lambda on: toggles.append(on))
    started = threading.Event()
    finished = threading.Event()
    response = {}

    def stale_toggle():
        started.set()
        response.update(voice.rpc(
            owner, "voice.toggle", action="tts", session_id="owner"
        ))
        finished.set()

    with server._voice_lifecycle_lock:
        thread = threading.Thread(target=stale_toggle)
        thread.start()
        assert started.wait(1)
        assert not finished.wait(0.1)
        assert server._release_voice_for_transport(owner) is True
        enabled = voice.rpc(
            successor, "voice.toggle", action="on", session_id="successor"
        )
        assert "error" not in enabled

    assert finished.wait(1)
    thread.join(timeout=1)
    assert response["error"]["code"] == 4016
    assert toggles == []
    assert server._voice_authority_transport is successor


def test_stale_tts_warm_worker_cannot_reacquire_after_release(monkeypatch):
    workers = []
    holders = set()
    operations = []

    def start_worker(target, **_kwargs):
        workers.append(target)

    def acquire(lease):
        operations.append(("acquire", lease))
        holders.add(lease)

    def release(lease):
        operations.append(("release", lease))
        holders.discard(lease)

    monkeypatch.setattr(server, "_voice_tts_lease_generation", 0, raising=False)
    monkeypatch.setattr(server, "_voice_tts_lease_desired", {}, raising=False)
    monkeypatch.setattr(server, "_start_voice_thread", start_worker)
    monkeypatch.setattr("tools.tts_tool_lifecycle.acquire_tts_lease", acquire)
    monkeypatch.setattr("tools.tts_tool_lifecycle.release_tts_lease", release)

    server._tts_lease_async("tui:voice-tts", True)
    server._tts_lease_async("tui:voice-tts", False)
    workers[1]()
    workers[0]()

    assert operations == [("release", "tui:voice-tts")]
    assert holders == set()


def test_authorized_voice_tts_cannot_fall_back_to_native_after_disconnect(
    voice, monkeypatch,
):
    owner = _Sink()
    voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    queued = []
    run_now = False
    spoken = []

    def start_worker(target, *, args=(), **_kwargs):
        if run_now:
            target(*args)
        else:
            queued.append((target, args))

    monkeypatch.setattr(server, "_start_voice_thread", start_worker)
    monkeypatch.setattr(
        voice.mic, "speak_text", lambda text, *_args: spoken.append(text), raising=False
    )

    response = voice.rpc(
        owner, "voice.tts", text="stale reply", session_id="owner"
    )
    assert "error" not in response
    assert len(queued) == 1

    run_now = True
    assert server._release_voice_for_transport(owner) is True
    target, args = queued[0]
    target(*args)

    assert spoken == []


def test_unrelated_voice_tts_is_refused_while_owner_is_idle(voice, monkeypatch):
    owner, other = _Sink(), _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    released, error = server._release_voice_lease(
        session_id="owner",
        lease_epoch=enabled["result"]["lease_epoch"],
        transport=owner,
        stop_loop=False,
    )
    assert released is True and error == ""
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {"barge_in": False}})

    refused = voice.rpc(other, "voice.tts", text="not yours", session_id="other")

    assert refused["error"]["code"] == 4016


def test_unrelated_session_cannot_stop_or_replace_the_owner(voice):
    owner, other = _Sink(), _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")

    stopped = voice.rpc(other, "voice.record", action="stop", session_id="other")
    replaced = voice.rpc(other, "voice.record", action="start", session_id="other")

    assert stopped["error"]["code"] == 4016
    assert replaced["error"]["code"] == 4016
    assert voice.mic.stop_calls == 0
    assert server._voice_lease_owner_sid == "owner"
    assert server._voice_lease_epoch == started["result"]["lease_epoch"]


def test_initial_full_duplex_arm_acquires_and_passes_captured_scope(voice, monkeypatch):
    owner = _Sink()
    started = []

    class _Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr(server.threading, "Thread", _Thread)
    monkeypatch.setattr(server, "_fd_listener_active", False)
    assert server._install_voice_authority(
        "owner", owner, allow_unclaimed=True
    )

    server._arm_full_duplex_listener("owner", owner)

    assert server._voice_lease_owner_sid == "owner"
    assert server._voice_lease_owner_transport is owner
    assert server._voice_lease_epoch > 0
    assert len(started) == 1
    target, args = started[0]
    assert callable(target)
    assert args == ("owner", server._voice_lease_epoch, owner)


def test_full_duplex_trip_only_interrupts_captured_session(voice, monkeypatch):
    owner = _Sink()
    owner_hits, other_hits, playback_stops = [], [], []
    monkeypatch.setattr(server, "_sessions", {
        "owner": {"running": True, "agent": types.SimpleNamespace(interrupt=lambda: owner_hits.append(True))},
        "other": {"running": True, "agent": types.SimpleNamespace(interrupt=lambda: other_hits.append(True))},
    })
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: playback_stops.append("tts"))
    _set_fake_playback(monkeypatch, playback_stops)
    epoch = server._acquire_voice_lease("owner", owner)

    assert server._fd_trip("generation", "owner", epoch, owner) is True
    assert owner_hits == [True]
    assert other_hits == []
    assert playback_stops == ["tts", True]


def test_stale_full_duplex_trigger_has_no_side_effects_after_reacquire(voice, monkeypatch):
    first, replacement = _Sink(), _Sink()
    hits, playback_stops = [], []
    epoch = server._acquire_voice_lease("first", first)
    released, error = server._release_voice_lease(
        session_id="first", lease_epoch=epoch, transport=first, stop_loop=False)
    assert released is True and error == ""
    replacement_epoch = server._acquire_voice_lease("replacement", replacement)
    assert replacement_epoch > epoch
    monkeypatch.setattr(server, "_sessions", {
        "first": {"running": True, "agent": types.SimpleNamespace(interrupt=lambda: hits.append("first"))},
        "replacement": {"running": True, "agent": types.SimpleNamespace(interrupt=lambda: hits.append("replacement"))},
    })
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: playback_stops.append("tts"))
    _set_fake_playback(monkeypatch, playback_stops)

    assert server._fd_trip("generation", "first", epoch, first) is False
    assert hits == []
    assert playback_stops == []


def test_interrupting_another_session_does_not_cut_voice_owners_tts(voice, monkeypatch):
    owner = _Sink()
    epoch = server._acquire_voice_lease("owner", owner)
    assert epoch
    session = {
        "history_lock": threading.RLock(),
        "running": True,
        "agent": types.SimpleNamespace(interrupt=lambda: None),
    }
    tts_stops, interrupted = [], []
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(
        server, "_interrupt_session_turn",
        lambda sid, _session, **_kw: interrupted.append(sid),
    )
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: tts_stops.append(True))

    result = server._methods["session.interrupt"]("interrupt", {"session_id": "other"})

    assert result["result"]["status"] == "interrupted"
    assert interrupted == ["other"]
    assert tts_stops == []


def test_same_session_new_transport_interrupt_cannot_cut_or_resume_voice_owner(
    voice, monkeypatch
):
    owner, replacement = _Sink(), _Sink()
    assert server._claim_voice_authority("owner", owner, allow_unclaimed=True)
    monkeypatch.setattr(server, "_voice_wake_owner", owner)
    session = {
        "history_lock": threading.RLock(),
        "running": True,
        "agent": types.SimpleNamespace(interrupt=lambda: None),
    }
    tts_stops, wake_resumes = [], []
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kwargs: tts_stops.append(True))
    monkeypatch.setattr(server, "_wake_resume_if_owner", wake_resumes.append)

    token = bind_transport(replacement)
    try:
        response = server._methods["session.interrupt"](
            "interrupt", {"session_id": "owner"}
        )
    finally:
        reset_transport(token)

    assert response["result"]["status"] == "interrupted"
    assert tts_stops == []
    assert wake_resumes == []


def test_release_blocks_reacquire_until_physical_close_then_acks(voice):
    owner, other = _Sink(), _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    release_started, allow_release = threading.Event(), threading.Event()

    def blocking_stop(**_kwargs):
        release_started.set()
        assert allow_release.wait(2)
        voice.mic.active = False

    voice.mic.stop_impl = blocking_stop
    reply = {}

    def stop_owner():
        reply.update(voice.rpc(owner, "voice.record", action="stop", session_id="owner"))

    worker = threading.Thread(target=stop_owner)
    worker.start()
    assert release_started.wait(1)
    refused = voice.rpc(other, "voice.record", action="start", session_id="other")
    assert refused["error"]["code"] == 4016
    allow_release.set()
    worker.join(2)

    assert reply["result"]["status"] == "stopped"
    assert reply["result"]["owner_session_id"] == "owner"
    assert reply["result"]["lease_epoch"] == started["result"]["lease_epoch"]
    assert reply["result"]["released"] is True
    assert reply["result"]["release_complete"] is True
    assert server._voice_lease_owner_sid == ""


def test_forced_stop_delivers_only_the_captured_epochs_transcript_tail(voice):
    owner = _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    callbacks = dict(voice.mic.callbacks)

    stopped = voice.rpc(owner, "voice.record", action="stop", session_id="owner")
    callbacks["on_transcript"]("final words")

    payload = _events(owner, "voice.transcript")[-1]["payload"]
    assert payload["text"] == "final words"
    assert payload["owner_session_id"] == "owner"
    assert payload["lease_epoch"] == started["result"]["lease_epoch"]
    assert stopped["result"]["release_complete"] is True

    restarted = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    callbacks["on_transcript"]("stale words")
    assert restarted["result"]["lease_epoch"] > started["result"]["lease_epoch"]
    assert [event["payload"]["text"] for event in _events(owner, "voice.transcript")] == [
        "final words"
    ]


def test_forced_stop_defers_idle_completion_until_physical_release(voice, monkeypatch):
    from tools import voice_input

    owner = _Sink()
    voice.rpc(owner, "voice.record", action="start", session_id="owner")
    callbacks = dict(voice.mic.callbacks)
    release_entered, allow_release = threading.Event(), threading.Event()

    def stop_with_early_idle(**_kwargs):
        voice.mic.active = False
        callbacks["on_status"]("idle")

    def blocking_release(**_kwargs):
        release_entered.set()
        assert allow_release.wait(2)
        return True

    voice.mic.stop_impl = stop_with_early_idle
    monkeypatch.setattr(voice_input, "release_voice_input", blocking_release)
    response = {}
    worker = threading.Thread(
        target=lambda: response.update(
            voice.rpc(owner, "voice.record", action="stop", session_id="owner")
        )
    )
    worker.start()
    assert release_entered.wait(1)
    assert _events(owner, "voice.status") == []

    allow_release.set()
    worker.join(2)

    idle = _events(owner, "voice.status")[-1]["payload"]
    assert idle["state"] == "idle"
    assert idle["release_complete"] is True
    assert response["result"]["release_complete"] is True


def test_toggle_off_cleanup_finishes_before_successor_can_acquire(voice, monkeypatch):
    owner, replacement = _Sink(), _Sink()
    voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    cleanup_entered, allow_cleanup = threading.Event(), threading.Event()

    def blocking_tts_off(on):
        assert on is False
        cleanup_entered.set()
        assert allow_cleanup.wait(2)

    monkeypatch.setattr(server, "_set_voice_tts", blocking_tts_off)
    response = {}
    worker = threading.Thread(
        target=lambda: response.update(
            voice.rpc(owner, "voice.toggle", action="off", session_id="owner")
        )
    )
    worker.start()
    assert cleanup_entered.wait(1)
    try:
        assert server._acquire_voice_lease("replacement", replacement) is None
    finally:
        allow_cleanup.set()
        worker.join(2)

    assert response["result"]["release_complete"] is True
    assert server._acquire_voice_lease("replacement", replacement) is not None


def test_session_teardown_releases_its_physical_voice_lease(voice, monkeypatch):
    owner, replacement = _Sink(), _Sink()
    stopped = []
    voice.rpc(owner, "voice.record", action="start", session_id="owner")
    session = {"_sid": "owner", "_run_thread": None}
    monkeypatch.setattr(server, "_settle_isolated_turn_before_close", lambda _session: None)
    monkeypatch.setattr(server, "_teardown_session", lambda _session, **_kwargs: None)
    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(
            stop_playback=lambda: stopped.append("playback"),
            stop_thinking_sound=lambda: stopped.append("thinking"),
        ),
    )

    assert server._teardown_popped_session(session) is True

    assert voice.mic.stop_calls == 1
    assert server._voice_lease_owner_sid == ""
    assert server._voice_authority_sid == ""
    assert server._voice_mode_enabled() is False
    assert server._voice_tts_enabled() is False
    assert stopped == ["thinking", "playback"]
    assert server._acquire_voice_lease("replacement", replacement) is not None


def test_stale_session_teardown_cannot_release_same_sid_successor_epoch(
    voice, monkeypatch
):
    owner = _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    old_epoch = enabled["result"]["lease_epoch"]
    released, error = server._release_voice_lease(
        session_id="owner", lease_epoch=old_epoch, transport=owner, stop_loop=False
    )
    assert released is True and error == ""
    original_authority_snapshot = server._voice_authority_snapshot
    successor_epoch = []

    def handoff_after_teardown_snapshot():
        authority = original_authority_snapshot()
        successor_epoch.append(
            server._claim_voice_authority("owner", owner, allow_unclaimed=True)
        )
        return authority

    monkeypatch.setattr(server, "_voice_authority_snapshot", handoff_after_teardown_snapshot)
    monkeypatch.setattr(server, "_settle_isolated_turn_before_close", lambda _session: None)
    monkeypatch.setattr(server, "_teardown_session", lambda _session, **_kwargs: None)

    assert server._teardown_popped_session(
        {"_sid": "owner", "_run_thread": None}
    ) is True

    assert successor_epoch[0] > old_epoch
    assert server._voice_lease_epoch == successor_epoch[0]
    assert server._voice_lease_owner_transport is owner
    assert server._voice_authority_transport is owner
    assert server._voice_mode_enabled() is True


def test_unrelated_session_cannot_start_or_cut_streaming_tts(voice, monkeypatch):
    owner, other = _Sink(), _Sink()
    assert server._acquire_voice_lease("owner", owner)
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    cuts, starts = [], []
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kwargs: cuts.append(True))
    monkeypatch.setattr(server, "_arm_barge_listener_if_enabled", lambda *_args: None)
    monkeypatch.setitem(
        sys.modules, "tools.tts_tool",
        types.SimpleNamespace(check_tts_requirements=lambda: True),
    )
    monkeypatch.setitem(
        sys.modules, "tools.tts_tool_speaker",
        types.SimpleNamespace(
            stream_tts_to_speaker=lambda *_args: starts.append("stream")
        ),
    )

    assert server._tts_stream_begin("other", other) is None
    assert cuts == []
    assert starts == []


def test_unrelated_session_cannot_start_fallback_tts_or_turn_audio(voice, monkeypatch):
    owner, other = _Sink(), _Sink()
    assert server._acquire_voice_lease("owner", owner)
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    spoken, armed, thinking, stream_begins = [], [], [], []

    class InlineThread:
        def __init__(self, *, target, args=(), **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    monkeypatch.setattr(
        voice.mic, "speak_text", lambda text, *_args: spoken.append(text), raising=False
    )
    monkeypatch.setattr(
        server, "_tts_stream_begin",
        lambda *_args: stream_begins.append(True),
    )
    monkeypatch.setattr(
        server, "_arm_full_duplex_listener", lambda *_args: armed.append(True)
    )
    monkeypatch.setitem(
        sys.modules, "tools.voice_mode",
        types.SimpleNamespace(
            is_audio_output_active=lambda: False,
            start_thinking_sound=lambda **_kwargs: thinking.append(True),
        ),
    )

    server._speak_text_with_barge("not yours", "other", other)
    turn_audio = server._start_turn_voice("other", other)

    assert turn_audio == (None, False)
    assert spoken == []
    assert armed == []
    assert thinking == []
    assert stream_begins == []


def test_reconnect_same_session_waits_for_old_transport_physical_release(voice):
    old, replacement = _Sink(), _Sink()
    voice.rpc(old, "voice.toggle", action="on", session_id="owner")
    release_started, allow_release = threading.Event(), threading.Event()

    def blocking_stop(**_kwargs):
        release_started.set()
        assert allow_release.wait(2)
        voice.mic.active = False

    voice.mic.stop_impl = blocking_stop
    released = []
    worker = threading.Thread(
        target=lambda: released.append(server._release_voice_for_transport(old))
    )
    worker.start()
    assert release_started.wait(1)

    refused = voice.rpc(
        replacement, "voice.toggle", action="on", session_id="owner"
    )
    assert refused["error"]["code"] == 4016

    allow_release.set()
    worker.join(2)
    assert released == [True]
    restarted = voice.rpc(
        replacement, "voice.toggle", action="on", session_id="owner"
    )
    assert restarted["result"]["enabled"] is True
    assert restarted["result"]["owner_session_id"] == "owner"


def test_disconnect_cleanup_is_consumed_by_overlapping_direct_release(
    voice, monkeypatch
):
    from tools import voice_input

    owner = _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    release_entered, allow_release = threading.Event(), threading.Event()

    def blocking_release(**_kwargs):
        release_entered.set()
        assert allow_release.wait(2)
        return True

    monkeypatch.setattr(voice_input, "release_voice_input", blocking_release)
    direct_result, disconnect_result = [], []
    direct = threading.Thread(
        target=lambda: direct_result.append(
            server._release_voice_lease(
                session_id="owner",
                lease_epoch=enabled["result"]["lease_epoch"],
                transport=owner,
                stop_loop=False,
            )
        )
    )
    direct.start()
    assert release_entered.wait(1)
    disconnect = threading.Thread(
        target=lambda: disconnect_result.append(
            server._release_voice_for_transport(owner)
        )
    )
    disconnect.start()
    allow_release.set()
    direct.join(2)
    disconnect.join(2)

    assert direct_result == [(True, "")]
    assert disconnect_result == [False]
    assert server._voice_mode_enabled() is False
    assert server._voice_tts_enabled() is False
    assert server._voice_authority_transport is None


def test_disconnect_claim_after_consume_is_rechecked_before_owner_exposure(
    voice, monkeypatch
):
    owner = _Sink()
    enabled = voice.rpc(owner, "voice.toggle", action="on", session_id="owner")
    real_consume = server._consume_pending_voice_disconnect
    first_consume, allow_finalize = threading.Event(), threading.Event()
    calls = 0

    def blocking_consume(*args):
        nonlocal calls
        result = real_consume(*args)
        calls += 1
        if calls == 1:
            first_consume.set()
            assert allow_finalize.wait(2)
        return result

    monkeypatch.setattr(server, "_consume_pending_voice_disconnect", blocking_consume)
    direct_result = []
    direct = threading.Thread(
        target=lambda: direct_result.append(
            server._release_voice_lease(
                session_id="owner",
                lease_epoch=enabled["result"]["lease_epoch"],
                transport=owner,
                stop_loop=False,
            )
        )
    )
    direct.start()
    assert first_consume.wait(1)

    assert server._release_voice_for_transport(owner) is False
    allow_finalize.set()
    direct.join(2)

    assert direct_result == [(True, "")]
    assert server._voice_pending_disconnect_scope is None
    assert server._voice_lease_owner_transport is None
    assert server._voice_authority_transport is None


def test_unleased_typed_stop_physically_clears_all_legacy_capture_streams(
    voice, monkeypatch
):
    from tools import voice_input

    voice.mic.active = True
    releases = []
    monkeypatch.setattr(
        voice_input,
        "release_voice_input",
        lambda **kwargs: releases.append(kwargs) or True,
    )

    assert server._consume_typed_stop_phrase("stop", "owner", emit=False) is True

    assert voice.mic.stop_calls == 1
    assert voice.mic.active is False
    assert releases[-1]["exclusive_only"] is False
    assert server._voice_lease_owner_sid == ""


def test_unleased_record_stop_physically_clears_all_legacy_capture_streams(
    voice, monkeypatch
):
    from tools import voice_input

    owner = _Sink()
    voice.mic.active = True
    releases = []
    monkeypatch.setattr(
        voice_input,
        "release_voice_input",
        lambda **kwargs: releases.append(kwargs) or True,
    )

    stopped = voice.rpc(owner, "voice.record", action="stop", session_id="owner")

    assert stopped["result"]["release_complete"] is True
    assert voice.mic.active is False
    assert releases[-1]["exclusive_only"] is False


def test_typed_stop_from_rebound_transport_cannot_release_same_session(voice):
    owner, replacement = _Sink(), _Sink()
    voice.rpc(owner, "voice.record", action="start", session_id="owner")

    token = bind_transport(replacement)
    try:
        stopped = server._consume_typed_stop_phrase("stop", "owner", emit=False)
    finally:
        reset_transport(token)

    assert stopped is False
    assert voice.mic.stop_calls == 0
    assert server._voice_lease_owner_transport is owner


def test_stale_stop_callback_cannot_release_successor_epoch(voice):
    owner = _Sink()
    old_epoch = server._acquire_voice_lease("owner", owner)
    released, error = server._release_voice_lease(
        session_id="owner", lease_epoch=old_epoch, transport=owner, stop_loop=False
    )
    assert released is True and error == ""
    successor_epoch = server._acquire_voice_lease("owner", owner)

    server._vr_on_stop_phrase("stop", ("owner", old_epoch, owner, None))

    assert successor_epoch > old_epoch
    assert server._voice_lease_epoch == successor_epoch
    assert server._voice_lease_owner_transport is owner


def test_real_lease_releases_unleased_capture_before_installing_callbacks(voice):
    voice.mic.active = True
    voice.mic.callbacks = {"legacy": True}
    owner = _Sink()

    response = voice.rpc(owner, "voice.record", action="start", session_id="owner")

    assert response["result"]["status"] == "recording"
    assert voice.mic.operations[:2] == ["stop", "start"]
    assert "on_transcript" in voice.mic.callbacks


def test_legacy_handoff_does_not_make_scoped_stop_release_shared_streams(
    voice, monkeypatch
):
    from tools import voice_input

    owner = _Sink()
    voice.mic.active = True
    exclusive = []
    monkeypatch.setattr(
        voice_input,
        "release_voice_input",
        lambda *, exclusive_only, **_kwargs: exclusive.append(exclusive_only) or True,
    )

    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    stopped = voice.rpc(owner, "voice.record", action="stop", session_id="owner")

    assert started["result"]["status"] == "recording"
    assert stopped["result"]["status"] == "stopped"
    assert exclusive == [False, True]


def test_unleased_start_failure_cleanup_still_releases_all_shared_streams(
    voice, monkeypatch
):
    from tools import voice_input
    from tools.voice_input import VoiceInputReleaseError

    owner = _Sink()
    voice.mic.active = True
    exclusive = []

    def release_input(*, exclusive_only, **_kwargs):
        exclusive.append(exclusive_only)
        if len(exclusive) == 1:
            raise VoiceInputReleaseError("legacy stream still closing")
        return True

    monkeypatch.setattr(voice_input, "release_voice_input", release_input)

    response = voice.rpc(owner, "voice.record", action="start", session_id="owner")

    assert response["error"]["code"] == 5025
    assert exclusive == [False, False]
    assert server._voice_lease_owner_transport is None


def test_release_failure_emits_scoped_error_without_false_ack_or_wake_resume(voice, monkeypatch):
    from tools.voice_input import VoiceInputReleaseError

    owner = _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    resumes = []
    wake_owner = object()
    monkeypatch.setattr(server, "_voice_wake_owner", wake_owner)
    monkeypatch.setattr(server, "_resume_voice_wake", lambda: resumes.append(True))
    voice.mic.stop_impl = lambda **_kw: (_ for _ in ()).throw(
        VoiceInputReleaseError("parec process did not release"))

    response = voice.rpc(owner, "voice.record", action="stop", session_id="owner")

    assert response["error"]["code"] == 5025
    payload = _events(owner, "voice.input_error")[-1]["payload"]
    assert payload["owner_session_id"] == "owner"
    assert payload["lease_epoch"] == started["result"]["lease_epoch"]
    assert payload["stage"] == "close"
    assert payload["released"] is False
    assert payload["release_complete"] is False
    assert server._voice_lease_owner_sid == "owner"
    assert server._voice_wake_owner is wake_owner
    assert resumes == []


def test_typed_stop_release_failure_is_consumed_as_rpc_error(voice, monkeypatch):
    from tools import voice_input
    from tools.voice_input import VoiceInputReleaseError

    owner = _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    monkeypatch.setattr(
        voice_input,
        "release_voice_input",
        lambda **_kwargs: (_ for _ in ()).throw(
            VoiceInputReleaseError("capture still closing")
        ),
    )
    monkeypatch.setattr(server, "_schedule_voice_release_retry", lambda *_args: None)

    response = voice.rpc(
        owner, "prompt.submit", session_id="owner", text="stop"
    )

    assert response["error"]["code"] == 5025
    payload = _events(owner, "voice.input_error")[-1]["payload"]
    assert payload["lease_epoch"] == started["result"]["lease_epoch"]
    assert payload["released"] is False


def test_release_failure_keeps_same_owner_restart_fenced_for_reaper(voice, monkeypatch):
    from tools.voice_input import VoiceInputReleaseError

    owner = _Sink()
    voice.rpc(owner, "voice.record", action="start", session_id="owner")
    retries = []
    monkeypatch.setattr(
        server,
        "_schedule_voice_release_retry",
        lambda sid, epoch, transport: retries.append((sid, epoch, transport)),
    )
    voice.mic.stop_impl = lambda **_kw: (_ for _ in ()).throw(
        VoiceInputReleaseError("still closing")
    )

    stopped = voice.rpc(owner, "voice.record", action="stop", session_id="owner")
    restarted = voice.rpc(owner, "voice.record", action="start", session_id="owner")

    assert stopped["error"]["code"] == 5025
    assert restarted["error"]["code"] == 4016
    assert server._voice_lease_releasing is True
    assert len(retries) == 1


def test_handoff_release_failure_reports_old_scope_before_refusing_new_start(
    voice, monkeypatch
):
    from tools import voice_input
    from tools.voice_input import VoiceInputReleaseError

    shared = _Sink()
    enabled = voice.rpc(shared, "voice.toggle", action="on", session_id="old")
    monkeypatch.setattr(
        voice_input,
        "release_voice_input",
        lambda **_kwargs: (_ for _ in ()).throw(
            VoiceInputReleaseError("voice input release is still in progress")
        ),
    )
    monkeypatch.setattr(server, "_schedule_voice_release_retry", lambda *_args: None)

    response = voice.rpc(
        shared, "voice.record", action="start", session_id="successor"
    )

    assert response["error"]["code"] == 5025
    payload = _events(shared, "voice.input_error")[-1]["payload"]
    assert payload["owner_session_id"] == "old"
    assert payload["lease_epoch"] == enabled["result"]["lease_epoch"]
    assert payload["released"] is False
    assert server._voice_lease_owner_sid == "old"
    assert server._voice_lease_releasing is True


def test_handoff_during_existing_release_is_busy_without_false_input_error(voice):
    shared = _Sink()
    voice.rpc(shared, "voice.toggle", action="on", session_id="old")
    server._voice_lease_releasing = True

    response = voice.rpc(
        shared, "voice.record", action="start", session_id="successor"
    )

    assert response["error"]["code"] == 4016
    assert _events(shared, "voice.input_error") == []
    assert server._voice_lease_owner_sid == "old"


def test_release_fence_suppresses_full_duplex_trigger_side_effects(voice, monkeypatch):
    from tools import voice_input

    owner = _Sink()
    epoch = server._acquire_voice_lease("owner", owner)
    release_entered, allow_release = threading.Event(), threading.Event()
    side_effects = []

    def blocking_release(**_kwargs):
        release_entered.set()
        assert allow_release.wait(2)
        return True

    monkeypatch.setattr(voice_input, "release_voice_input", blocking_release)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: side_effects.append("tts"))
    _set_fake_playback(monkeypatch, side_effects)
    worker = threading.Thread(
        target=lambda: server._release_voice_lease(
            session_id="owner", lease_epoch=epoch, transport=owner, stop_loop=False
        )
    )
    worker.start()
    assert release_entered.wait(1)

    assert server._fd_trip("playback", "owner", epoch, owner) is False
    assert side_effects == []

    allow_release.set()
    worker.join(2)


def test_voice_scope_lookup_never_holds_sid_lock_while_waiting_for_sessions(
    voice, monkeypatch
):
    """Pin the lock graph: voice scope lookup never waits for the sessions lock."""
    real_lock = threading.RLock()
    session_held = threading.Event()
    release_sessions = threading.Event()

    class SignalingSessionsLock:
        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *_exc):
            real_lock.release()

    sessions_lock = SignalingSessionsLock()
    monkeypatch.setattr(server, "_sessions_lock", sessions_lock)

    other = _Sink()
    assert server._install_voice_authority("owner", other, allow_unclaimed=True)

    def hold_sessions():
        with sessions_lock:
            session_held.set()
            assert release_sessions.wait(1)

    holder = threading.Thread(target=hold_sessions, daemon=True)
    holder.start()
    assert session_held.wait(1)
    resolver = threading.Thread(
        target=lambda: server._full_duplex_scope("owner", other), daemon=True
    )
    resolver.start()
    resolver.join(1)
    assert not resolver.is_alive(), "voice scope lookup waited for sessions lock"
    release_sessions.set()
    holder.join(1)
    assert not holder.is_alive()


def test_full_duplex_open_barrier_rejects_scope_released_before_provider_open(
    voice, monkeypatch
):
    owner = _Sink()
    reached_guard, continue_open, listener_done = (
        threading.Event(), threading.Event(), threading.Event()
    )
    allowed = []

    def fake_listen(
        _should_stop, *, start_barrier, on_input_error=None, **_kwargs
    ):
        reached_guard.set()
        assert continue_open.wait(2)
        with start_barrier() as can_start:
            allowed.append(can_start)
        listener_done.set()

    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(
            full_duplex_listen=fake_listen,
            is_audio_output_active=lambda: False,
            transcribe_recording=lambda _path: {"success": True, "transcript": ""},
        ),
    )
    monkeypatch.setattr(server, "_fd_listener_active", False)
    assert server._install_voice_authority(
        "owner", owner, allow_unclaimed=True
    )
    server._arm_full_duplex_listener("owner", owner)
    epoch = server._voice_lease_epoch
    assert reached_guard.wait(1)

    released, error = server._release_voice_lease(
        session_id="owner", lease_epoch=epoch, transport=owner, stop_loop=False
    )
    assert released is True and error == ""
    continue_open.set()
    assert listener_done.wait(1)
    assert allowed == [False]


def test_full_duplex_release_waits_for_atomic_open_barrier(voice, monkeypatch):
    owner = _Sink()
    open_entered, allow_open, release_started, release_done = (
        threading.Event(), threading.Event(), threading.Event(), threading.Event()
    )

    def fake_listen(_should_stop, *, start_barrier, **_kwargs):
        with start_barrier() as can_start:
            assert can_start is True
            open_entered.set()
            assert allow_open.wait(2)

    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(
            full_duplex_listen=fake_listen,
            is_audio_output_active=lambda: False,
            transcribe_recording=lambda _path: {"success": True, "transcript": ""},
        ),
    )
    assert server._install_voice_authority(
        "owner", owner, allow_unclaimed=True
    )
    server._arm_full_duplex_listener("owner", owner)
    epoch = server._voice_lease_epoch
    assert open_entered.wait(1)
    release_result = {}

    def release():
        release_started.set()
        release_result["value"] = server._release_voice_lease(
            session_id="owner", lease_epoch=epoch, transport=owner, stop_loop=False
        )
        release_done.set()

    worker = threading.Thread(target=release)
    worker.start()
    assert release_started.wait(1)
    assert release_done.wait(0.05) is False
    allow_open.set()
    worker.join(2)

    assert release_done.is_set()
    assert release_result["value"] == (True, "")


def test_async_eof_is_scoped_and_released_before_wake_resume(voice, monkeypatch):
    from tools.voice_input import VoiceInputEOF

    owner = _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    order = []
    monkeypatch.setattr(server, "_resume_voice_wake", lambda: order.append("wake"))
    voice.mic.callbacks["on_input_error"](VoiceInputEOF("parec ended"))

    event = _events(owner, "voice.input_error")[-1]
    payload = event["payload"]
    assert payload["owner_session_id"] == "owner"
    assert payload["lease_epoch"] == started["result"]["lease_epoch"]
    assert payload["stage"] == "read"
    assert payload["released"] is True
    assert payload["release_complete"] is True
    assert order == ["wake"]
    assert server._voice_lease_owner_sid == ""


def test_full_duplex_natural_idle_emits_scoped_output_drained(voice, monkeypatch):
    owner = _Sink()
    epoch = server._acquire_voice_lease("owner", owner)
    monkeypatch.setattr(server, "_sessions", {"owner": {"running": False}})
    monkeypatch.setattr(server, "_fd_tts_pending", lambda: False)
    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(
            full_duplex_listen=lambda *_args, **_kwargs: None,
            is_audio_output_active=lambda: False,
            transcribe_recording=lambda _path: {"success": True, "transcript": ""},
        ),
    )

    server._full_duplex_listener("owner", epoch, owner)

    payload = _events(owner, "voice.output_drained")[-1]["payload"]
    assert payload == {"owner_session_id": "owner", "lease_epoch": epoch}


def test_full_duplex_empty_interruption_requests_scoped_record_retry(
    voice, monkeypatch, tmp_path
):
    owner = _Sink()
    epoch = server._acquire_voice_lease("owner", owner)
    wav = tmp_path / "empty.wav"
    wav.write_bytes(b"wav")
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: None)

    def listen(_should_stop, *, on_trigger, **_kwargs):
        on_trigger("playback")
        return str(wav)

    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(
            full_duplex_listen=listen,
            is_audio_output_active=lambda: False,
            stop_playback=lambda: None,
            transcribe_recording=lambda _path: {"success": True, "transcript": ""},
        ),
    )

    server._full_duplex_listener("owner", epoch, owner)

    payload = _events(owner, "voice.transcript")[-1]["payload"]
    assert payload["text"] == ""
    assert payload["retryable_empty"] is True
    assert payload["outcome"] == "empty"
    assert payload["owner_session_id"] == "owner"
    assert payload["lease_epoch"] == epoch


def test_output_drain_monitor_waits_without_opening_microphone(voice, monkeypatch):
    owner = _Sink()
    epoch = server._acquire_voice_lease("owner", owner)
    session = {"running": True}
    state = {"tts": True, "audio": True}
    monkeypatch.setattr(server, "_sessions", {"owner": session})
    monkeypatch.setattr(server, "_fd_tts_pending", lambda: state["tts"])
    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(is_audio_output_active=lambda: state["audio"]),
    )

    server._arm_output_drain_monitor("owner", owner)
    assert _events(owner, "voice.output_drained") == []
    session["running"] = False
    state.update(tts=False, audio=False)
    wait_until(
        lambda: bool(_events(owner, "voice.output_drained")),
        timeout=1,
        what="scoped output drain",
    )
    payload = _events(owner, "voice.output_drained")[-1]["payload"]
    assert payload["owner_session_id"] == "owner"
    assert payload["lease_epoch"] == epoch


def test_dead_owner_release_retries_until_physical_input_is_free(voice, monkeypatch):
    from tools import voice_input
    from tools.voice_input import VoiceInputReleaseError

    owner = _Sink()
    started = voice.rpc(owner, "voice.record", action="start", session_id="owner")
    owner._closed = True
    calls = []

    def flaky_release(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise VoiceInputReleaseError("still closing")
        return True

    monkeypatch.setattr(voice_input, "release_voice_input", flaky_release)
    monkeypatch.setattr(server, "_VOICE_RELEASE_RETRY_SECONDS", 0.01, raising=False)

    assert server._release_voice_for_transport(owner) is False
    wait_until(
        lambda: not server._voice_lease_owner_sid,
        timeout=1,
        what="dead transport voice input release retry",
    )

    assert len(calls) >= 2
    assert server._voice_lease_owner_sid == ""
    assert server._voice_lease_epoch == started["result"]["lease_epoch"]


def test_successor_cannot_acquire_before_prior_retry_worker_exits(voice, monkeypatch):
    from tools import voice_input
    from tools.voice_input import VoiceInputReleaseError

    owner, successor = _Sink(), _Sink()
    epoch = server._acquire_voice_lease("owner", owner)
    release_calls = []
    retry_resuming = threading.Event()
    allow_retry_exit = threading.Event()

    def flaky_release(**_kwargs):
        release_calls.append(True)
        if len(release_calls) == 1:
            raise VoiceInputReleaseError("initial close failed")
        return True

    def blocking_resume():
        retry_resuming.set()
        allow_retry_exit.wait(timeout=1)

    monkeypatch.setattr(voice_input, "release_voice_input", flaky_release)
    monkeypatch.setattr(server, "_resume_voice_wake", blocking_resume)
    monkeypatch.setattr(server, "_VOICE_RELEASE_RETRY_SECONDS", 0.01, raising=False)

    released, _error = server._release_voice_lease(
        session_id="owner", lease_epoch=epoch, transport=owner, stop_loop=False
    )
    assert released is False
    assert retry_resuming.wait(timeout=1)
    assert server._voice_lease_owner_transport is None
    assert server._voice_release_retry_active is True
    try:
        assert server._acquire_voice_lease("successor", successor) is None
    finally:
        allow_retry_exit.set()
    wait_until(
        lambda: not server._voice_release_retry_active,
        timeout=1,
        what="prior release retry worker exit",
    )
    assert server._acquire_voice_lease("successor", successor) is not None


def test_disconnect_clears_logical_authority_during_retry_exit_tail(
    voice, monkeypatch
):
    from tools import voice_input
    from tools.voice_input import VoiceInputReleaseError

    owner, successor = _Sink(), _Sink()
    epoch = server._claim_voice_authority("owner", owner, allow_unclaimed=True)
    release_calls = []
    retry_resuming = threading.Event()
    allow_retry_exit = threading.Event()

    def flaky_release(**_kwargs):
        release_calls.append(True)
        if len(release_calls) == 1:
            raise VoiceInputReleaseError("initial close failed")
        return True

    def blocking_resume():
        retry_resuming.set()
        allow_retry_exit.wait(timeout=1)

    monkeypatch.setattr(voice_input, "release_voice_input", flaky_release)
    monkeypatch.setattr(server, "_resume_voice_wake", blocking_resume)
    monkeypatch.setattr(server, "_VOICE_RELEASE_RETRY_SECONDS", 0.01, raising=False)

    released, _error = server._release_voice_lease(
        session_id="owner", lease_epoch=epoch, transport=owner, stop_loop=False
    )
    assert released is False
    assert retry_resuming.wait(timeout=1)
    assert server._voice_lease_owner_transport is None
    assert server._voice_authority_transport is owner
    try:
        assert server._release_voice_for_transport(
            owner, expected_session_id="owner", expected_epoch=epoch
        ) is True
        assert server._voice_authority_transport is None
        assert server._voice_mode_enabled() is False
        assert server._acquire_voice_lease("successor", successor) is None
    finally:
        allow_retry_exit.set()
    wait_until(
        lambda: not server._voice_release_retry_active,
        timeout=1,
        what="logical-tail release retry worker exit",
    )
    assert server._acquire_voice_lease("successor", successor) is not None


def test_wake_stop_reports_physical_release_truth_and_retains_failed_owner(voice, monkeypatch):
    from tools import wake_word

    owner = _Sink()
    monkeypatch.setattr(server, "_wake_owner_transport", owner)
    monkeypatch.setattr(server, "_wake_owner_surface", "tui")
    monkeypatch.setattr(
        wake_word, "stop_listening", lambda **_kw: (_ for _ in ()).throw(OSError("busy"))
    )

    failed = voice.rpc(owner, "wake.stop")
    assert failed["result"]["stopped"] is False
    assert failed["result"]["released"] is False
    assert failed["result"]["release_complete"] is False
    assert server._wake_owner_transport is owner

    monkeypatch.setattr(wake_word, "stop_listening", lambda **_kw: True)
    stopped = voice.rpc(owner, "wake.stop")
    assert stopped["result"]["stopped"] is True
    assert stopped["result"]["released"] is True
    assert stopped["result"]["release_complete"] is True
    assert server._wake_owner_transport is None


def test_wake_diagnostics_rpc_is_strict_and_owner_scoped(voice, monkeypatch):
    from tools import wake_word

    owner = _Sink()
    resets = []
    monkeypatch.setattr(server, "_wake_owner_transport", owner)
    monkeypatch.setattr(server, "_wake_owner_profile_home", None)

    def diagnostics(*, owner, reset=False):
        resets.append(reset)
        return {
            "armed": True,
            "owned_by_caller": True,
            "reason": None,
            "capture": "client",
            "external_audio": True,
            "processed_frames": 0,
            "pcm_peak": {"latest": None, "peak": None},
            "client_queue": {"depth": 0, "drops": 0},
        }

    monkeypatch.setattr(
        wake_word,
        "get_wake_diagnostics",
        diagnostics,
        raising=False,
    )

    response = voice.rpc(owner, "wake.diagnostics", reset=True)
    assert response["result"]["owned_by_caller"] is True
    assert resets == [True]
    invalid = voice.rpc(owner, "wake.diagnostics", reset="yes")
    assert invalid["error"]["code"] == 4001
