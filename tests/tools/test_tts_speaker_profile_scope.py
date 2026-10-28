"""Profile isolation across the streaming speaker's background threads."""

import threading


def test_streaming_prefetch_and_playback_keep_the_callers_profile(monkeypatch, tmp_path):
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from tools import tts_tool_speaker as speaker

    launch = tmp_path / "launch"
    worker = tmp_path / "worker"
    launch.mkdir()
    worker.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    streamed = []
    played = []

    class _LazyStreamer:
        sample_rate = 16_000
        channels = 1

        def stream(self, _text):
            streamed.append(get_hermes_home())
            yield b"\x01\x00"

    def fake_play(chunks, _stop, _rate):
        list(chunks)
        played.append(get_hermes_home())

    monkeypatch.setattr(
        speaker._StreamerPlayback, "_device_usable", lambda _self: False
    )
    monkeypatch.setattr(speaker, "_play_via_tempfile", fake_play)
    token = set_hermes_home_override(worker)
    try:
        playback = speaker._StreamerPlayback(_LazyStreamer(), threading.Event())
        playback.speak("hello")
    finally:
        reset_hermes_home_override(token)
    playback.finish()

    assert streamed == [worker]
    assert played == [worker]
    assert launch not in streamed + played
