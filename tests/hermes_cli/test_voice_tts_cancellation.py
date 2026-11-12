"""Cancellation fences for whole-file TTS fallback playback."""

import json
import threading

import pytest


@pytest.mark.real_audio_playback
def test_cancel_during_sync_synthesis_prevents_late_playback(monkeypatch):
    from hermes_cli import voice
    from tools import tts_tool

    synthesis_started = threading.Event()
    allow_synthesis = threading.Event()
    stop = threading.Event()
    played = []
    output_path = "/tmp/hermes_voice/cancelled.mp3"

    def synthesize(**_kwargs):
        synthesis_started.set()
        assert allow_synthesis.wait(2)
        return json.dumps({
            "success": True,
            "file_path": output_path,
            "file_paths": [output_path],
        })

    monkeypatch.setattr(voice, "_continuous_active", False)
    monkeypatch.setattr(voice, "_continuous_recorder", None)
    monkeypatch.setattr(voice, "_speak_streaming", lambda *_args: False)
    monkeypatch.setattr(tts_tool, "text_to_speech_tool", synthesize)
    monkeypatch.setattr(voice.os, "makedirs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(voice.os.path, "isfile", lambda path: path == output_path)
    monkeypatch.setattr(voice.os.path, "getsize", lambda _path: 1000)
    monkeypatch.setattr(voice.os, "unlink", lambda _path: None)
    monkeypatch.setattr(voice, "play_audio_file", lambda path: played.append(path))

    thread = threading.Thread(target=voice.speak_text, args=("late reply", stop))
    thread.start()
    assert synthesis_started.wait(1)
    stop.set()
    allow_synthesis.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert played == []
