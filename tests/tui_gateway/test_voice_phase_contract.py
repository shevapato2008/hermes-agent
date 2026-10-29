"""SmartBox voice phases keep one logical authority across physical captures."""

from __future__ import annotations

import sys
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.transport import FanoutTransport, bind_transport, reset_transport


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
        self.stop_impl = None

    def start_continuous(self, **kwargs):
        self.active = True
        self.callbacks = kwargs
        return True

    def stop_continuous(self, **kwargs):
        if self.stop_impl is not None:
            return self.stop_impl(**kwargs)
        self.active = False

    def is_continuous_active(self):
        return self.active

    def set_voice_busy_probe(self, _probe):
        pass


@pytest.fixture
def phase_voice(monkeypatch):
    mic, stdio = _Mic(), _Sink()
    monkeypatch.setitem(sys.modules, "hermes_cli.voice", mic)
    monkeypatch.setenv("HERMES_VOICE", "1")
    monkeypatch.setenv("HERMES_VOICE_TTS", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {}})
    monkeypatch.setattr(server, "_session_voice_enabled", lambda _sid: True)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kw: None)
    monkeypatch.setattr(server, "_stdio_transport", stdio)
    monkeypatch.setattr(server, "_current_voice_profile_home", lambda: None)
    for name, value in {
        "_voice_event_sid": "",
        "_voice_event_transport": None,
        "_voice_capture_sid": "",
        "_voice_capture_gen": 0,
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
        "_wake_resume_retry_active": False,
        "_wake_resume_retry_epoch": None,
        "_voice_tts_lease_generation": 0,
        "_voice_tts_lease_desired": {},
        "_fd_listener_active": False,
        "_fd_listener_epoch": 0,
        "_output_drain_stop": None,
        "_output_drain_epoch": 0,
        "_sessions": {},
    }.items():
        monkeypatch.setattr(server, name, value, raising=False)
    from tools import voice_input

    monkeypatch.setattr(voice_input, "release_voice_input", lambda **_kw: True)

    def rpc(transport, method, **params):
        token = bind_transport(transport)
        try:
            return server.handle_request(
                {"id": method, "method": method, "params": params}
            )
        finally:
            reset_transport(token)

    return types.SimpleNamespace(mic=mic, rpc=rpc)


def _events(sink, kind):
    return [
        frame["params"]
        for frame in sink.frames
        if frame.get("params", {}).get("type") == kind
    ]


def test_silent_ordinary_capture_reports_no_speech_after_release(phase_voice):
    owner = _Sink()
    started = phase_voice.rpc(
        owner, "voice.record", action="start", session_id="runtime"
    )

    phase_voice.mic.callbacks["on_status"]("transcribing")
    phase_voice.mic.callbacks["on_status"]("idle")

    payload = _events(owner, "voice.status")[-1]["payload"]
    assert payload["state"] == "idle"
    assert payload["outcome"] == "no_speech"
    assert payload["released"] is True
    assert payload["release_complete"] is True
    assert payload["capture_id"] == started["result"]["capture_id"]


def test_silent_capture_relisten_announces_the_ordinary_successor_epoch(
    phase_voice, monkeypatch
):
    owner = _Sink()
    first = phase_voice.rpc(
        owner, "voice.record", action="start", session_id="runtime"
    )
    phase_voice.mic.active = False
    phase_voice.mic.callbacks["on_status"]("idle")
    terminal = _events(owner, "voice.status")[-1]["payload"]
    assert terminal["outcome"] == "no_speech"
    assert terminal["lease_epoch"] == first["result"]["lease_epoch"]
    assert _events(owner, "voice.lease_changed") == []

    original_start = phase_voice.mic.start_continuous
    transitions_at_start = []

    def start_continuous(**kwargs):
        transitions_at_start.extend(_events(owner, "voice.lease_changed"))
        return original_start(**kwargs)

    monkeypatch.setattr(phase_voice.mic, "start_continuous", start_continuous)
    second = phase_voice.rpc(
        owner, "voice.record", action="start", session_id="runtime"
    )

    assert second["result"]["lease_epoch"] > first["result"]["lease_epoch"]
    assert second["result"]["previous_lease_epoch"] == first["result"]["lease_epoch"]
    assert second["result"]["capture_id"] != first["result"]["capture_id"]
    assert [event["payload"] for event in transitions_at_start] == [
        {
            "owner_session_id": "runtime",
            "lease_epoch": second["result"]["lease_epoch"],
            "previous_lease_epoch": first["result"]["lease_epoch"],
            "phase": "ordinary_record",
        }
    ]


def test_record_to_full_duplex_announces_successor_epoch_before_listener(
    phase_voice, monkeypatch
):
    owner = _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    started = phase_voice.rpc(
        owner, "voice.record", action="start", session_id="runtime"
    )
    assert started["result"]["lease_epoch"] == enabled["result"]["lease_epoch"]
    phase_voice.mic.callbacks["on_transcript"]("hello")
    phase_voice.mic.callbacks["on_status"]("idle")
    assert _events(owner, "voice.status")[-1]["payload"]["outcome"] == "complete"

    starts = []

    def start_thread(target, *, name, args=()):
        transition = _events(owner, "voice.lease_changed")
        assert transition, "the successor epoch must be published before its listener starts"
        starts.append((target, name, args))
        return types.SimpleNamespace()

    monkeypatch.setattr(server, "_start_voice_thread", start_thread)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda *_args: None)
    server._start_turn_voice("runtime", owner)

    payload = _events(owner, "voice.lease_changed")[-1]["payload"]
    assert payload == {
        "owner_session_id": "runtime",
        "lease_epoch": server._voice_lease_epoch,
        "previous_lease_epoch": enabled["result"]["lease_epoch"],
        "phase": "full_duplex",
    }
    assert payload["lease_epoch"] > payload["previous_lease_epoch"]
    assert starts[0][2] == ("runtime", payload["lease_epoch"], owner)
    assert server._arm_full_duplex_listener("runtime", owner) is True
    assert len(_events(owner, "voice.lease_changed")) == 1
    assert len(starts) == 1


def test_transcript_releases_before_turn_claims_a_successor_epoch(
    phase_voice, monkeypatch
):
    owner = _Sink()
    first = phase_voice.rpc(
        owner, "voice.record", action="start", session_id="runtime"
    )
    old_idle = phase_voice.mic.callbacks["on_status"]
    starts = []
    monkeypatch.setattr(server, "_tts_stream_begin", lambda *_args: None)
    monkeypatch.setattr(
        server,
        "_start_voice_thread",
        lambda target, *, name, args=(): starts.append((name, args))
        or types.SimpleNamespace(),
    )
    turn_threads = []
    turn_results = []
    original_write = owner.write

    def write(frame):
        written = original_write(frame)
        if frame.get("params", {}).get("type") == "voice.transcript":
            thread = threading.Thread(
                target=lambda: turn_results.append(
                    server._start_turn_voice("runtime", owner)
                )
            )
            turn_threads.append(thread)
            thread.start()
        return written

    monkeypatch.setattr(owner, "write", write)
    phase_voice.mic.callbacks["on_transcript"]("hello")
    assert len(turn_threads) == 1
    turn_threads[0].join(2)
    assert not turn_threads[0].is_alive()

    # The recorder's old idle may arrive after prompt.submit has already armed
    # the turn listener. It must be stale and unable to release that successor.
    old_idle("idle")

    transitions = _events(owner, "voice.lease_changed")
    assert len(transitions) == 1
    payload = transitions[0]["payload"]
    assert payload == {
        "owner_session_id": "runtime",
        "lease_epoch": server._voice_lease_epoch,
        "previous_lease_epoch": first["result"]["lease_epoch"],
        "phase": "full_duplex",
    }
    assert payload["lease_epoch"] > first["result"]["lease_epoch"]
    assert server._voice_lease_owner_sid == "runtime"
    assert server._voice_lease_owner_transport is owner
    assert starts == [
        ("voice-full-duplex", ("runtime", payload["lease_epoch"], owner))
    ]
    assert len(turn_results) == 1


def test_forced_stop_transcript_waits_for_inflight_physical_release(
    phase_voice, monkeypatch
):
    from tools import voice_input

    owner = _Sink()
    phase_voice.rpc(owner, "voice.record", action="start", session_id="runtime")
    callback = phase_voice.mic.callbacks["on_transcript"]
    callback_started = threading.Event()
    callback_done = threading.Event()
    release_started = threading.Event()
    allow_release = threading.Event()

    def deliver_tail():
        callback_started.set()
        callback("final words")
        callback_done.set()

    callback_thread = threading.Thread(target=deliver_tail, name="forced-stop-tail")

    def stop_and_transcribe(**_kwargs):
        phase_voice.mic.active = False
        callback_thread.start()

    def blocking_release(**_kwargs):
        release_started.set()
        assert allow_release.wait(1)
        return True

    phase_voice.mic.stop_impl = stop_and_transcribe
    monkeypatch.setattr(voice_input, "release_voice_input", blocking_release)
    stop_reply = {}
    stop_thread = threading.Thread(
        target=lambda: stop_reply.update(
            phase_voice.rpc(
                owner, "voice.record", action="stop", session_id="runtime"
            )
        ),
        name="forced-stop",
    )
    stop_thread.start()
    assert callback_started.wait(1)
    assert release_started.wait(1)
    assert not callback_done.wait(0.1)

    allow_release.set()
    stop_thread.join(1)
    callback_thread.join(1)

    assert stop_reply["result"]["release_complete"] is True
    assert [event["payload"]["text"] for event in _events(owner, "voice.transcript")] == [
        "final words"
    ]
    assert _events(owner, "voice.input_error") == []


def test_unowned_capture_stop_fences_same_transport_successor_start(
    phase_voice, monkeypatch
):
    owner = _Sink()
    phase_voice.rpc(owner, "voice.record", action="start", session_id="runtime")
    phase_voice.mic.callbacks["on_transcript"]("first turn")
    assert server._voice_lease_owner_transport is None
    assert phase_voice.mic.active is True

    successor_checked = threading.Event()
    successor_done = threading.Event()
    successor_reply = {}
    original_capture_live = server._capture_live

    def capture_live():
        if threading.current_thread().name == "successor-record":
            successor_checked.set()
        return original_capture_live()

    monkeypatch.setattr(server, "_capture_live", capture_live)

    def start_successor():
        successor_reply.update(
            phase_voice.rpc(
                owner, "voice.record", action="start", session_id="successor"
            )
        )
        successor_done.set()

    successor = threading.Thread(target=start_successor, name="successor-record")

    def stop_old_wrapper(**_kwargs):
        # The old wrapper finishes naturally after its active probe.  A same-
        # transport successor may now pass its preflight, but must not claim or
        # start until this old stop has returned through the lifecycle barrier.
        phase_voice.mic.active = False
        successor.start()
        assert successor_checked.wait(1)
        assert not successor_done.wait(0.1)

    phase_voice.mic.stop_impl = stop_old_wrapper
    assert server._stop_session_voice_capture("runtime", transport=owner) is True
    successor.join(1)

    assert not successor.is_alive()
    assert successor_reply["result"]["status"] == "recording"
    assert server._voice_lease_owner_sid == "successor"
    assert phase_voice.mic.active is True


def test_record_to_output_drain_announces_successor_epoch_before_monitor(
    phase_voice, monkeypatch
):
    owner = _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    phase_voice.rpc(owner, "voice.record", action="start", session_id="runtime")
    phase_voice.mic.callbacks["on_transcript"]("hello")
    phase_voice.mic.callbacks["on_status"]("idle")

    starts = []

    def start_thread(target, *, name, args=()):
        transition = _events(owner, "voice.lease_changed")
        assert transition, "the successor epoch must be published before its monitor starts"
        starts.append((target, name, args))
        return types.SimpleNamespace()

    monkeypatch.setattr(server, "_start_voice_thread", start_thread)
    assert server._arm_output_drain_monitor("runtime", owner) is True

    payload = _events(owner, "voice.lease_changed")[-1]["payload"]
    assert payload == {
        "owner_session_id": "runtime",
        "lease_epoch": server._voice_lease_epoch,
        "previous_lease_epoch": enabled["result"]["lease_epoch"],
        "phase": "output_drain",
    }
    assert payload["lease_epoch"] > payload["previous_lease_epoch"]
    assert starts[0][1] == "voice-output-drain"
    assert starts[0][2][:3] == ("runtime", payload["lease_epoch"], owner)
    assert server._arm_output_drain_monitor("runtime", owner) is True
    assert len(_events(owner, "voice.lease_changed")) == 1
    assert len(starts) == 1


@pytest.mark.parametrize(
    ("arm_name", "phase"),
    [
        ("_arm_full_duplex_listener", "full_duplex"),
        ("_arm_output_drain_monitor", "output_drain"),
    ],
)
def test_concurrent_successor_arms_cannot_lose_the_epoch_transition(
    phase_voice, monkeypatch, arm_name, phase
):
    owner = _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    old_epoch = enabled["result"]["lease_epoch"]
    released, error = server._release_voice_lease(
        session_id="runtime",
        lease_epoch=old_epoch,
        transport=owner,
        stop_loop=False,
    )
    assert released is True and error == ""

    predecessor_claimed = threading.Event()
    expose_predecessor = threading.Event()
    original_scope = server._full_duplex_scope

    def gated_scope(*args, **kwargs):
        scope = original_scope(*args, **kwargs)
        if threading.current_thread().name == "predecessor-arm":
            predecessor_claimed.set()
            assert expose_predecessor.wait(2)
        return scope

    starts = []
    monkeypatch.setattr(server, "_full_duplex_scope", gated_scope)
    monkeypatch.setattr(
        server,
        "_start_voice_thread",
        lambda target, *, name, args=(): starts.append((name, args))
        or types.SimpleNamespace(),
    )
    arm = getattr(server, arm_name)
    results = []
    first = threading.Thread(
        target=lambda: results.append(arm("runtime", owner)),
        name="predecessor-arm",
    )
    second = threading.Thread(
        target=lambda: results.append(arm("runtime", owner)),
        name="successor-arm",
    )
    first.start()
    assert predecessor_claimed.wait(1)
    second.start()
    expose_predecessor.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert results == [True, True]
    transitions = _events(owner, "voice.lease_changed")
    assert len(transitions) == 1
    assert transitions[0]["payload"] == {
        "owner_session_id": "runtime",
        "lease_epoch": server._voice_lease_epoch,
        "previous_lease_epoch": old_epoch,
        "phase": phase,
    }
    assert len(starts) == 1


def test_voice_tts_announces_the_successor_epoch_before_scheduling_speech(
    phase_voice, monkeypatch
):
    owner = _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    old_epoch = enabled["result"]["lease_epoch"]
    released, error = server._release_voice_lease(
        session_id="runtime",
        lease_epoch=old_epoch,
        transport=owner,
        stop_loop=False,
    )
    assert released is True and error == ""
    scheduled = []

    def start_thread(target, *, name, args=()):
        transitions = _events(owner, "voice.lease_changed")
        assert transitions, "the successor epoch must be published before speech is scheduled"
        scheduled.append((name, args))
        return types.SimpleNamespace()

    monkeypatch.setattr(server, "_start_voice_thread", start_thread)
    response = phase_voice.rpc(
        owner, "voice.tts", text="hello", session_id="runtime"
    )

    payload = _events(owner, "voice.lease_changed")[-1]["payload"]
    assert payload == {
        "owner_session_id": "runtime",
        "lease_epoch": response["result"]["lease_epoch"],
        "previous_lease_epoch": old_epoch,
        "phase": "full_duplex",
    }
    assert scheduled[0][0] == "voice-tts-rpc"


@pytest.mark.parametrize(
    ("source", "barge_in", "expected"),
    [
        ("smartbox_voice", True, "full_duplex"),
        ("smartbox_voice", False, "output_drain"),
        ("desktop", True, None),
    ],
)
def test_toggle_on_rearms_running_smartbox_session_exactly_once(
    phase_voice, monkeypatch, source, barge_in, expected
):
    owner = _Sink()
    monkeypatch.setenv("HERMES_VOICE_TTS", "0")
    monkeypatch.setattr(
        server,
        "_sessions",
        {"runtime": {"running": True, "source": source, "transport": owner}},
    )
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {"voice": {"barge_in": barge_in}}
    )
    armed = []
    monkeypatch.setattr(
        server,
        "_start_voice_thread",
        lambda target, *, name, args=(): armed.append((name, args))
        or types.SimpleNamespace(),
    )

    enabled = phase_voice.rpc(owner, "voice.toggle", action="on", session_id="runtime")
    assert "error" not in enabled
    assert armed == [], "toggle on must return its epoch before any resumed worker starts"
    first = phase_voice.rpc(
        owner, "voice.toggle", action="reattach", session_id="runtime"
    )
    second = phase_voice.rpc(
        owner, "voice.toggle", action="reattach", session_id="runtime"
    )

    if expected is None:
        assert first["error"]["code"] == 4016
        assert second["error"]["code"] == 4016
        assert armed == []
        return
    for response in (first, second):
        result = response["result"]
        assert result["reattached"] is True
        assert result["phase"] == expected
        assert result["owner_session_id"] == "runtime"
        assert result["lease_epoch"] == enabled["result"]["lease_epoch"]
    thread_name = (
        "voice-full-duplex" if expected == "full_duplex" else "voice-output-drain"
    )
    assert len(armed) == 1
    assert armed[0][0] == thread_name
    assert armed[0][1][:3] == (
        "runtime", enabled["result"]["lease_epoch"], owner
    )


def test_reattach_accepts_the_exact_owner_inside_a_live_session_fanout(
    phase_voice, monkeypatch
):
    owner, viewer = _Sink(), _Sink()
    monkeypatch.setenv("HERMES_VOICE_TTS", "0")
    monkeypatch.setattr(
        server,
        "_sessions",
        {
            "runtime": {
                "running": True,
                "source": "smartbox_voice",
                "transport": FanoutTransport(viewer, owner),
                "profile_home": None,
            }
        },
    )
    armed = []
    monkeypatch.setattr(
        server,
        "_start_voice_thread",
        lambda target, *, name, args=(): armed.append((name, args))
        or types.SimpleNamespace(),
    )

    enabled = phase_voice.rpc(owner, "voice.toggle", action="on", session_id="runtime")
    response = phase_voice.rpc(
        owner, "voice.toggle", action="reattach", session_id="runtime"
    )

    assert response["result"]["reattached"] is True
    assert response["result"]["lease_epoch"] == enabled["result"]["lease_epoch"]
    assert armed == [
        (
            "voice-full-duplex",
            ("runtime", enabled["result"]["lease_epoch"], owner),
        )
    ]


def test_turn_voice_keeps_the_exact_owner_when_the_session_slot_becomes_a_fanout(
    phase_voice, monkeypatch
):
    owner, viewer = _Sink(), _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    lease_epoch = enabled["result"]["lease_epoch"]
    tts_queue = object()
    stream_transports = []
    listener_transports = []
    monkeypatch.setattr(
        server,
        "_tts_stream_begin",
        lambda _sid, transport: stream_transports.append(transport) or tts_queue,
    )
    monkeypatch.setattr(
        server,
        "_arm_full_duplex_listener",
        lambda _sid, transport: listener_transports.append(transport) or True,
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.voice_mode",
        types.SimpleNamespace(
            is_audio_output_active=lambda: False,
            start_thinking_sound=lambda **_kwargs: True,
        ),
    )

    assert server._start_turn_voice("runtime", owner) == (tts_queue, True)
    session = {"transport": owner}
    assert server._attach_session_transport(session, viewer) is True
    fanout = session["transport"]
    assert isinstance(fanout, FanoutTransport)
    assert server._start_turn_voice("runtime", fanout) == (tts_queue, True)
    assert server._detach_session_transport(session, viewer) is True
    assert session["transport"] is fanout
    assert server._start_turn_voice("runtime", fanout) == (tts_queue, True)

    assert stream_transports == [owner, owner, owner]
    assert listener_transports == [owner, owner, owner]
    assert server._voice_lease_owner_transport is owner
    assert server._voice_authority_transport is owner
    assert server._voice_lease_epoch == lease_epoch


def test_turn_voice_rejects_fanout_without_a_live_exact_owner(
    phase_voice, monkeypatch
):
    owner, viewer, unrelated = _Sink(), _Sink(), _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    lease_epoch = enabled["result"]["lease_epoch"]
    downstream = []
    monkeypatch.setattr(
        server,
        "_tts_stream_begin",
        lambda *_args: downstream.append("tts"),
    )
    monkeypatch.setattr(
        server,
        "_arm_full_duplex_listener",
        lambda *_args: downstream.append("listener") or True,
    )

    assert server._start_turn_voice("runtime", viewer) == (None, False)
    assert server._start_turn_voice(
        "runtime", FanoutTransport(viewer, unrelated)
    ) == (None, False)
    stale = FanoutTransport(owner, viewer)
    assert stale.detach(owner) is True
    assert server._start_turn_voice("runtime", stale) == (None, False)
    closed = FanoutTransport(owner, viewer)
    owner._closed = True
    assert server._start_turn_voice("runtime", closed) == (None, False)

    assert downstream == []
    assert server._voice_lease_owner_transport is owner
    assert server._voice_authority_transport is owner
    assert server._voice_lease_epoch == lease_epoch


def test_fallback_speech_arms_barge_in_with_the_exact_owner_from_a_fanout(
    phase_voice, monkeypatch
):
    owner, viewer = _Sink(), _Sink()
    enabled = phase_voice.rpc(
        owner, "voice.toggle", action="on", session_id="runtime"
    )
    lease_epoch = enabled["result"]["lease_epoch"]
    started = []
    armed = []
    monkeypatch.setattr(
        phase_voice.mic, "speak_text", lambda *_args: None, raising=False
    )
    monkeypatch.setattr(
        server,
        "_start_voice_thread",
        lambda target, *, name, args=(): started.append((target, name, args))
        or types.SimpleNamespace(),
    )
    monkeypatch.setattr(
        server,
        "_arm_barge_listener_if_enabled",
        lambda sid, transport: armed.append((sid, transport)),
    )

    server._speak_text_with_barge(
        "reply", "runtime", FanoutTransport(owner, viewer), True
    )

    assert len(started) == 1
    assert started[0][1:] == ("voice-speak", ())
    assert armed == [("runtime", owner)]
    assert server._voice_lease_owner_transport is owner
    assert server._voice_authority_transport is owner
    assert server._voice_lease_epoch == lease_epoch


def test_tts_enabled_parameter_is_an_idempotent_set(phase_voice, monkeypatch):
    owner = _Sink()
    phase_voice.rpc(owner, "voice.toggle", action="on", session_id="runtime")
    monkeypatch.setenv("HERMES_VOICE_TTS", "0")
    monkeypatch.setattr(server, "_tts_lease_async", lambda *_args: None)

    first = phase_voice.rpc(
        owner, "voice.toggle", action="tts", session_id="runtime", enabled=True
    )
    second = phase_voice.rpc(
        owner, "voice.toggle", action="tts", session_id="runtime", enabled=True
    )

    assert first["result"]["tts"] is True
    assert second["result"]["tts"] is True
    assert first["result"]["owner_session_id"] == "runtime"
    assert second["result"]["lease_epoch"] == first["result"]["lease_epoch"]
