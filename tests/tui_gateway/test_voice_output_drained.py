"""Tests for full-duplex voice output-drained and retryable-empty session fences."""

from __future__ import annotations

import sys
import types

import pytest

from tui_gateway import server
from tests.tui_gateway.test_tui_gateway_server import _fake_tts_modules


@pytest.mark.parametrize("triggered", [False, True], ids=["natural-idle", "triggered"])
def test_full_duplex_output_drained_uses_armed_session_after_natural_idle_return(
    monkeypatch, triggered
):
    """The listener releases the mic before reporting natural output drain;
    a VAD-triggered return must not report drain even if it is idle by then."""
    import tools.tts_streaming as ts

    monkeypatch.setenv("HERMES_VOICE", "1")
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {"barge_in": True}})
    monkeypatch.setattr(ts, "mark_speech_interrupted", lambda: None)
    monkeypatch.setattr(server, "_voice_event_sid", "sid-global-drift")
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_tts_stream_state", None)
    monkeypatch.setattr(server, "_fd_speak_pipelines", set())

    runtime = {"running": triggered, "inside_listen": False}
    monkeypatch.setattr(
        server, "_any_session_running", lambda: runtime["running"]
    )
    monkeypatch.setattr(server, "_fd_tts_pending", lambda: False)

    events: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: events.append(
            (event, sid, payload, runtime["inside_listen"])
        ),
    )

    def fake_listen(should_stop, is_playing=None, on_trigger=None, **_kw):
        runtime["inside_listen"] = True
        try:
            assert should_stop() is (not triggered)
            if triggered:
                on_trigger("generation")
                runtime["running"] = False
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
    server._arm_full_duplex_listener("sid-prompt")

    drained = [event for event in events if event[0] == "voice.output_drained"]
    if triggered:
        assert drained == []
    else:
        assert drained == [("voice.output_drained", "sid-prompt", {}, False)]


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
        assert lifecycle == {"inside_listen": False, "transcribed": True}
        emitted.append((event, sid, payload))

    voice_events = []
    monkeypatch.setattr(server, "_emit", fake_emit)
    monkeypatch.setattr(
        server,
        "_voice_emit",
        lambda event, payload=None: voice_events.append((event, payload)),
    )

    server._full_duplex_listener("sid-frozen")

    assert emitted == [
        (
            "voice.transcript",
            "sid-frozen",
            {"text": "", "retryable_empty": True},
        )
    ]
    assert [event for event in voice_events if event[0] == "voice.transcript"] == []
    assert not wav.exists()
