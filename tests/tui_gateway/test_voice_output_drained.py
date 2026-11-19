"""Tests for full-duplex voice output-drained and retryable-empty session fences."""

from __future__ import annotations

import sys
import types

import pytest

from tui_gateway import server
from tests.tui_gateway.test_tui_gateway_server import _fake_tts_modules


@pytest.fixture(autouse=True)
def _reset_voice_lease_state():
    def reset():
        if stop := getattr(server, "_output_drain_stop", None):
            stop.set()
        server._output_drain_stop = None
        server._voice_lease_owner_sid = ""
        server._voice_lease_owner_transport = None
        server._voice_authority_sid = ""
        server._voice_authority_transport = None
        server._voice_lease_epoch = 0
        server._voice_lease_releasing = False
        server._voice_lease_capture_id = None
        server._voice_release_tail_epoch = None
        server._voice_pending_idle_scope = None
        server._voice_terminal_error_epochs = set()
        server._voice_release_retry_active = False
        server._fd_listener_active = False
        server._fd_listener_epoch = 0

    reset()
    yield
    reset()


@pytest.mark.parametrize("triggered", [False, True], ids=["natural-idle", "triggered"])
def test_full_duplex_output_drained_uses_armed_session_after_natural_idle_return(
    monkeypatch, triggered
):
    """The listener releases the mic before reporting natural output drain;
    a VAD-triggered return must not report drain even if it is idle by then."""
    import tools.tts_streaming as ts

    owner = types.SimpleNamespace(_closed=False, send_line=lambda _line: None)
    epoch = server._acquire_voice_lease("sid-prompt", owner)

    monkeypatch.setenv("HERMES_VOICE", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {"barge_in": True}})
    monkeypatch.setattr(ts, "mark_speech_interrupted", lambda: None)
    monkeypatch.setattr(server, "_voice_event_sid", "sid-global-drift")
    runtime = {"running": triggered, "inside_listen": False}
    session_state = {"running": triggered}
    monkeypatch.setattr(server, "_sessions", {"sid-prompt": session_state})
    monkeypatch.setattr(server, "_tts_stream_state", None)
    monkeypatch.setattr(server, "_fd_speak_pipelines", set())
    monkeypatch.setattr(server, "_fd_tts_pending", lambda: False)

    events: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: events.append(
            (server.current_transport(), event, sid, payload, runtime["inside_listen"])
        ),
    )

    def fake_listen(should_stop, is_playing=None, on_trigger=None, **_kw):
        runtime["inside_listen"] = True
        try:
            assert should_stop() is (not triggered)
            if triggered:
                on_trigger("generation")
                runtime["running"] = False
                session_state["running"] = False
        finally:
            runtime["inside_listen"] = False

    _fake_tts_modules(monkeypatch, listen=fake_listen)

    class ImmediateThread:
        def __init__(self, *, target, args=(), **_kw):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    server._arm_full_duplex_listener("sid-prompt", owner)

    drained = [event for event in events if event[1] == "voice.output_drained"]
    if triggered:
        assert drained == []
    else:
        assert drained == [
            (
                owner,
                "voice.output_drained",
                "sid-prompt",
                {"owner_session_id": "sid-prompt", "lease_epoch": epoch},
                False,
            )
        ]


@pytest.mark.parametrize(
    ("transcript", "configured_wake"),
    [
        ("", "hey hermes"),
        ("HÉY—HERMÈS!", "héy hermès"),
    ],
    ids=["empty", "wake-only"],
)
def test_full_duplex_retryable_empty_fence_uses_frozen_session_after_listen_closes(
    monkeypatch, tmp_path, transcript, configured_wake
):
    owner = types.SimpleNamespace(_closed=False, send_line=lambda _line: None)
    epoch = server._acquire_voice_lease("sid-frozen", owner)

    monkeypatch.setenv("HERMES_VOICE", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {"barge_in": True}})
    monkeypatch.setattr(server, "_voice_event_sid", "sid-before-listen")

    wav = tmp_path / "retryable-empty.wav"
    wav.write_bytes(b"RIFF")
    lifecycle = {"inside_listen": False, "transcribed": False}

    def fake_listen(should_stop, is_playing=None, on_trigger=None, **_kw):
        lifecycle["inside_listen"] = True
        try:
            on_trigger("generation")
            return str(wav)
        finally:
            lifecycle["inside_listen"] = False

    def fake_transcribe(path, model=None):
        assert lifecycle["inside_listen"] is False
        assert path == str(wav)
        lifecycle["transcribed"] = True
        server._voice_event_sid = "sid-global-drift"
        return {"success": True, "transcript": transcript}

    _fake_tts_modules(monkeypatch, listen=fake_listen, transcribe=fake_transcribe)
    monkeypatch.setitem(
        sys.modules,
        "tools.wake_word",
        types.SimpleNamespace(
            load_wake_word_config=lambda: {"phrase": configured_wake},
            wake_phrase=lambda cfg: cfg["phrase"],
        ),
    )

    emitted = []

    def fake_emit(event, sid, payload=None):
        if event == "voice.transcript":
            assert lifecycle == {"inside_listen": False, "transcribed": True}
        emitted.append((server.current_transport(), event, sid, payload))

    monkeypatch.setattr(server, "_emit", fake_emit)

    server._full_duplex_listener("sid-frozen", epoch, owner)

    transcript_events = [e for e in emitted if e[1] == "voice.transcript"]
    assert transcript_events == [
        (
            owner,
            "voice.transcript",
            "sid-frozen",
            {
                "text": "",
                "retryable_empty": True,
                "outcome": "empty",
                "owner_session_id": "sid-frozen",
                "lease_epoch": epoch,
            },
        )
    ]
    assert not wav.exists()


def test_fd_trip_playback_interrupts_running_agent_and_marks_turn_cancel_requested(
    monkeypatch,
):
    import threading
    import tools.tts_streaming as ts

    owner = types.SimpleNamespace(_closed=False, send_line=lambda _line: None)
    epoch = server._acquire_voice_lease("sid-barge", owner)
    interrupted_calls = []
    fake_agent = types.SimpleNamespace(
        interrupt=lambda: interrupted_calls.append("interrupt"),
    )
    session_state = {
        "running": True,
        "agent": fake_agent,
        "history_lock": threading.RLock(),
    }
    monkeypatch.setattr(server, "_sessions", {"sid-barge": session_state})
    monkeypatch.setattr(ts, "mark_speech_interrupted", lambda: None)
    _fake_tts_modules(monkeypatch)
    emitted = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: emitted.append((event, sid, payload)),
    )

    assert server._fd_trip("playback", "sid-barge", epoch, owner) is True
    assert interrupted_calls == ["interrupt"]
    assert session_state.get("_turn_cancel_requested") is True
    assert any(ev == "voice.interrupted" for ev, _, _ in emitted)


def test_voice_record_start_same_scope_preempts_active_full_duplex_lease(monkeypatch):
    owner = types.SimpleNamespace(
        _closed=False, send_line=lambda _line: None, write=lambda _obj: True
    )
    monkeypatch.setenv("HERMES_VOICE", "1")
    epoch = server._claim_voice_authority("sid-preempt", owner, allow_unclaimed=True)
    server._fd_listener_active = True
    server._fd_listener_epoch = epoch

    release_calls = []
    orig_release = server._release_voice_lease

    def tracking_release(**kwargs):
        release_calls.append(kwargs)
        return orig_release(**kwargs)

    monkeypatch.setattr(server, "_release_voice_lease", tracking_release)
    monkeypatch.setattr(server, "_capture_live", lambda: False)
    started_captures = []
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.voice",
        types.SimpleNamespace(
            start_continuous=lambda on_transcript, **kw: started_captures.append(kw),
            stop_continuous=lambda: None,
            is_active=lambda: False,
        ),
    )

    token = server.bind_transport(owner)
    try:
        resp = server._methods["voice.record"](
            "req-1", {"action": "start", "session_id": "sid-preempt"}
        )
    finally:
        server.reset_transport(token)

    assert "result" in resp, f"Expected recording result, got error: {resp}"
    assert resp["result"]["status"] == "recording"
    assert resp["result"]["previous_lease_epoch"] == epoch
    assert resp["result"]["lease_epoch"] > epoch
    assert len(release_calls) == 1
    assert release_calls[0]["session_id"] == "sid-preempt"
    assert release_calls[0]["lease_epoch"] == epoch
    assert server._fd_listener_active is False


def test_auto_continue_keeps_same_text_barge_in_when_inflight_interrupted_and_drain_clears_cancel(
    monkeypatch,
):
    import threading

    fake_agent = types.SimpleNamespace(_interrupt_requested=False)
    session = {
        "running": True,
        "agent": fake_agent,
        "history_lock": threading.RLock(),
        "inflight_turn": {"user": "继续"},
        "_turn_cancel_requested": True,
    }
    transport = types.SimpleNamespace(
        _closed=False, send_line=lambda _line: None, write=lambda _obj: True
    )
    env = server._enqueue_prompt(session, "继续", transport)
    assert env is not None
    assert session["queued_prompt"]["text"] == "继续"
    server._drop_queued_duplicates_of_inflight_user(session)
    assert session["queued_prompt"] is not None
    assert session["queued_prompt"]["text"] == "继续"

    # When _drain_queued_prompt claims the queued prompt on idle transition, it resets _turn_cancel_requested
    session["running"] = False
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _s: False)
    monkeypatch.setattr(server, "_replace_queued_user_row_for_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_a, **_k: None)
    assert server._drain_queued_prompt("drain-1", "sid-ac", session) is True
    assert session.get("_turn_cancel_requested") is False


def test_send_tts_eof_enqueues_none_sentinel_once_and_preserves_tts_queue():
    import queue
    from tui_gateway import prompt_turn

    q: queue.Queue = queue.Queue()
    st = prompt_turn._TurnRun(
        agent=types.SimpleNamespace(),
        one_turn_restore=None,
        terminal_callback=None,
        receipt_committed=True,
        tts_queue=q,
    )
    assert getattr(st, "tts_eof_sent", False) is False
    prompt_turn._send_tts_eof(st)
    assert st.tts_eof_sent is True
    assert st.tts_queue is q
    assert q.get_nowait() is None
    assert q.empty()

    # Second call (e.g. in _release_turn_scopes) must be a no-op
    prompt_turn._send_tts_eof(st)
    assert q.empty()
    assert st.tts_queue is q


