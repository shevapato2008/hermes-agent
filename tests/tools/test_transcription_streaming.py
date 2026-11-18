"""Live STT sessions: wire behaviour over a real socket, and the recorder hand-off."""

from __future__ import annotations

import json
import threading

from websockets.sync.server import serve

from tools import transcription_streaming as ts
from tools import voice_mode


def _xai_like_server(received: list):
    """xAI ``/v1/stt`` shape: created first, a partial per audio frame, stitched final on audio.done."""

    def handler(ws):
        ws.send(json.dumps({"type": "transcript.created"}))
        words = []
        for raw in ws:
            if isinstance(raw, bytes):
                received.append(raw)
                words.append(f"w{len(words)}")
                ws.send(json.dumps({"type": "transcript.partial", "text": " ".join(words),
                                    "is_final": False, "speech_final": False}))
            elif json.loads(raw).get("type") == "audio.done":
                ws.send(json.dumps({"type": "transcript.partial", "text": " ".join(words),
                                    "is_final": True, "speech_final": True}))
                ws.send(json.dumps({"type": "transcript.done", "text": ""}))
                return

    server = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_live_session_streams_partials_before_end_and_keeps_early_audio():
    received: list = []
    server = _xai_like_server(received)
    port = server.socket.getsockname()[1]
    partials: list = []
    seen_partial = threading.Event()

    def on_partial(text):
        partials.append(text)
        seen_partial.set()

    session = ts.XAIStreamingSession("k", f"http://127.0.0.1:{port}/v1", "grok-voice-transcribe-2.0", "en",
                                     on_partial).start()
    session.push_audio(b"\x01\x00" * 160)  # before transcript.created: must be held, not dropped
    session.push_audio(b"\x02\x00" * 160)
    assert seen_partial.wait(5), "a partial must arrive while audio is still streaming"
    result = session.finalize(timeout=5)
    server.shutdown()

    assert result == {"success": True, "transcript": "w0 w1", "provider": "xai"}
    assert len(received) == 2
    assert partials[-1] == "w0 w1"


class _Session:
    def __init__(self, result):
        self.result, self.ended = result, False

    def end_audio(self):
        self.ended = True

    def finalize(self):
        return self.result

    def cancel(self):
        pass


def test_recording_uses_the_live_transcript_and_falls_back_to_the_file(tmp_path, monkeypatch):
    uploads: list = []
    monkeypatch.setattr("tools.transcription_tools.transcribe_audio",
                        lambda path, **kw: uploads.append(path) or {"success": True, "transcript": "from file"})
    live_wav, failed_wav = str(tmp_path / "a.wav"), str(tmp_path / "b.wav")

    live = _Session({"success": True, "transcript": "from live", "provider": "xai"})
    voice_mode._park_live_session(live_wav, live)
    assert live.ended, "parking ends the audio so the provider flushes while the caller works"
    assert voice_mode.transcribe_recording(live_wav)["transcript"] == "from live"
    assert uploads == []

    voice_mode._park_live_session(failed_wav, _Session({"success": False, "transcript": "", "error": "socket"}))
    assert voice_mode.transcribe_recording(failed_wav)["transcript"] == "from file"
    assert uploads == [failed_wav]


def test_openai_realtime_custom_base_url_streams_direct_16k_and_replaces_non_monotonic_text():
    partials: list[str] = []
    session = ts.OpenAIRealtimeSession(
        "secret",
        "http://127.0.0.1:8091/v1",
        "smartbox-sherpa-zh-en",
        "zh",
        None,
        on_partial=partials.append,
    )
    assert session._stream_rate == 16000
    assert session._resample is None

    sent_frames: list[dict] = []

    class _FakeWS:
        def send(self, raw: str) -> None:
            sent_frames.append(json.loads(raw))

    ws = _FakeWS()
    session._open(ws)
    assert sent_frames[0]["session"]["audio"]["input"]["format"]["rate"] == 16000

    raw_pcm = b"\x01\x02" * 480
    session._send_audio(ws, raw_pcm)
    import base64
    assert base64.b64decode(sent_frames[1]["audio"]) == raw_pcm

    session._event({"type": "conversation.item.input_audio_transcription.delta", "delta": "今天是", "text": "今天是"})
    session._event({"type": "conversation.item.input_audio_transcription.delta", "delta": "", "text": "今天十点"})
    assert partials == ["今天是", "今天十点"]
    done = session._event({"type": "conversation.item.input_audio_transcription.completed", "transcript": ""})
    assert done == {"success": True, "transcript": "今天十点", "provider": "openai"}


def test_openai_realtime_cloud_base_url_keeps_24k_resampler():
    session = ts.OpenAIRealtimeSession(
        "sk-test",
        "https://api.openai.com/v1",
        ts.DEFAULT_OPENAI_STREAMING_MODEL,
        "zh",
        None,
    )
    assert session._stream_rate == 24000
    assert session._resample is not None

