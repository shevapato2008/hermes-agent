"""Tests for wake.start surface/client_capture lease isolation and voice.tts_sentence."""

from __future__ import annotations

import pytest

from tests.tui_gateway.test_voice_input_lease import _Sink, voice as _base_voice
from tui_gateway import server


@pytest.fixture
def voice_rpc(voice):
    return voice


pytest_plugins = ["tests.tui_gateway.test_voice_input_lease"]
del _base_voice


def test_wake_start_tui_without_client_capture_forces_local_when_config_is_client(
    voice_rpc, monkeypatch
):
    from tools import wake_word

    tui_transport = _Sink()
    daemon_transport = _Sink()
    monkeypatch.setattr(
        wake_word,
        "load_wake_word_config",
        lambda: {
            "enabled": True,
            "capture": "client",
            "provider": "openwakeword",
            "phrase": "hey hermes",
            "surface": "auto",
        },
    )

    def fake_reqs(cfg):
        capture = cfg.get("capture")
        return {
            "available": capture == "client",
            "hint": "no local mic" if capture == "local" else "",
            "phrase": "hey hermes",
            "provider": "openwakeword",
            "capture": capture,
        }

    monkeypatch.setattr(wake_word, "check_wake_word_requirements", fake_reqs)
    started_external = []
    monkeypatch.setattr(
        wake_word,
        "start_listening",
        lambda _cb, *, owner, config, external_audio=False: started_external.append(
            (owner, external_audio)
        ),
    )
    monkeypatch.setattr(wake_word, "owns_listener", lambda owner: True)

    # Embedded TUI omits client_capture -> must probe local and refuse without stealing the lease.
    tui_resp = voice_rpc.rpc(tui_transport, "wake.start", surface="tui")
    assert tui_resp["result"]["started"] is False
    assert tui_resp["result"]["reason"] == "unavailable"
    assert tui_resp["result"]["capture"] == "local"
    assert server._wake_owner_transport is None

    # Native voice daemon explicitly passes client_capture=True -> arms client capture cleanly.
    daemon_resp = voice_rpc.rpc(
        daemon_transport, "wake.start", surface="tui", client_capture=True
    )
    assert daemon_resp["result"]["started"] is True
    assert daemon_resp["result"]["capture"] == "client"
    assert server._wake_owner_transport is daemon_transport
    assert started_external == [(daemon_transport, True)]
