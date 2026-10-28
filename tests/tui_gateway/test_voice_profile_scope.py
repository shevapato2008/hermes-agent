from pathlib import Path
import threading

from tui_gateway import server


def _bind_profiles(monkeypatch, tmp_path):
    launch = tmp_path / "launch"
    worker = tmp_path / "profiles" / "worker"
    launch.mkdir(parents=True)
    worker.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(
        server,
        "_profile_home",
        lambda name: worker if str(name or "").strip() == "worker" else None,
    )
    for name, value in {
        "_voice_lease_owner_sid": "",
        "_voice_lease_owner_transport": None,
        "_voice_lease_epoch": 0,
        "_voice_lease_releasing": False,
        "_voice_lease_capture_id": None,
        "_voice_lease_profile_home": None,
        "_voice_lease_release_all": False,
        "_voice_pending_disconnect_scope": None,
        "_voice_authority_sid": "",
        "_voice_authority_transport": None,
        "_voice_authority_profile_home": None,
        "_voice_wake_owner": None,
        "_wake_owner_transport": None,
        "_wake_owner_surface": "",
        "_wake_owner_profile_home": None,
        "_voice_release_retry_active": False,
    }.items():
        monkeypatch.setattr(server, name, value, raising=False)
    return launch, worker


def test_voice_status_reads_the_requested_profiles_voice_config(monkeypatch, tmp_path):
    launch, worker = _bind_profiles(monkeypatch, tmp_path)
    seen = []

    def load_cfg():
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        seen.append(home)
        key = "worker+b" if home == worker else "launch+b"
        return {"voice": {"record_key": key}}

    monkeypatch.setattr(server, "_load_cfg", load_cfg)
    monkeypatch.setattr(
        "tools.voice_mode.check_voice_requirements",
        lambda: {"available": True, "audio_available": True, "stt_available": True},
    )

    worker_response = server._methods["voice.toggle"](
        "worker", {"action": "status", "profile": "worker"}
    )
    launch_response = server._methods["voice.toggle"]("launch", {"action": "status"})

    assert worker_response["result"]["record_key"] == "worker+b"
    assert launch_response["result"]["record_key"] == "launch+b"
    assert seen == [worker, launch]


def test_wake_persistence_writes_the_requested_profile(monkeypatch, tmp_path):
    launch, worker = _bind_profiles(monkeypatch, tmp_path)
    reads = []
    writes = []

    def load_wake_config():
        from hermes_constants import get_hermes_home

        reads.append(Path(get_hermes_home()))
        return {"enabled": True}

    def persist(enabled):
        from hermes_constants import get_hermes_home

        writes.append((Path(get_hermes_home()), enabled))
        return True

    monkeypatch.setattr(
        server, "_release_wake_for_transport", lambda _transport, **_kwargs: False
    )
    monkeypatch.setattr("tools.wake_word.load_wake_word_config", load_wake_config)
    monkeypatch.setattr(server, "_persist_wake_enabled", persist)

    response = server._methods["wake.stop"](
        "worker", {"persist": True, "profile": "worker"}
    )

    assert response["result"]["disabled_persisted"] is True
    assert reads == [worker]
    assert writes == [(worker, False)]
    assert launch not in reads


def test_voice_tts_background_threads_keep_the_requested_profile(monkeypatch, tmp_path):
    launch, worker = _bind_profiles(monkeypatch, tmp_path)
    spoken = threading.Event()
    seen = []

    def speak_text(_text, _stop_event=None):
        from hermes_constants import get_hermes_home

        seen.append(Path(get_hermes_home()))
        spoken.set()

    monkeypatch.setenv("HERMES_VOICE", "0")
    monkeypatch.setattr("hermes_cli.voice.speak_text", speak_text)

    response = server._methods["voice.tts"](
        "worker", {"text": "hello", "profile": "worker"}
    )

    assert "error" not in response
    assert spoken.wait(2)
    assert seen == [worker]
    assert launch not in seen


def test_disconnect_release_restores_the_voice_owners_profile(monkeypatch, tmp_path):
    launch, worker = _bind_profiles(monkeypatch, tmp_path)
    transport = object()
    config_reads = []
    tts_released = []
    tts_done = threading.Event()

    def load_cfg():
        from hermes_constants import get_hermes_home

        config_reads.append(Path(get_hermes_home()))
        return {"voice": {"input": {"release_timeout_seconds": 0.1}}}

    def release_tts(_lease):
        from hermes_constants import get_hermes_home

        tts_released.append(Path(get_hermes_home()))
        tts_done.set()

    monkeypatch.setenv("HERMES_VOICE_TTS", "0")
    monkeypatch.setattr(server, "_stdio_transport", transport)
    monkeypatch.setattr(server, "_load_cfg", load_cfg)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kwargs: None)
    monkeypatch.setattr("tools.voice_input.release_voice_input", lambda **_kwargs: True)
    monkeypatch.setattr("tools.tts_tool_lifecycle.release_tts_lease", release_tts)

    response = server._methods["voice.toggle"](
        "worker", {"action": "on", "session_id": "worker-session", "profile": "worker"}
    )
    assert "error" not in response
    config_reads.clear()

    assert server._release_voice_for_transport(transport) is True
    assert tts_done.wait(2)
    assert config_reads == [worker]
    assert tts_released == [worker]
    assert launch not in config_reads + tts_released


def test_failed_disconnect_cleanup_and_retry_keep_the_voice_owners_profile(
    monkeypatch, tmp_path,
):
    launch, worker = _bind_profiles(monkeypatch, tmp_path)
    transport = object()
    config_reads = []
    cleanup_homes = []
    wake_homes = []
    cleanup_done = threading.Event()
    retry_done = threading.Event()
    attempts = 0

    def load_cfg():
        from hermes_constants import get_hermes_home

        config_reads.append(Path(get_hermes_home()))
        return {"voice": {"input": {"release_timeout_seconds": 0.1}}}

    def release_input(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("close failed once")
        return True

    def release_tts(_lease):
        from hermes_constants import get_hermes_home

        cleanup_homes.append(Path(get_hermes_home()))
        cleanup_done.set()

    def resume_wake(_owner):
        from hermes_constants import get_hermes_home

        wake_homes.append(Path(get_hermes_home()))
        retry_done.set()
        return True

    monkeypatch.setenv("HERMES_VOICE_TTS", "0")
    monkeypatch.setattr(server, "_stdio_transport", transport)
    monkeypatch.setattr(server, "_load_cfg", load_cfg)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_VOICE_RELEASE_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("tools.voice_input.release_voice_input", release_input)
    monkeypatch.setattr("tools.tts_tool_lifecycle.release_tts_lease", release_tts)
    monkeypatch.setattr(server, "_wake_resume_if_owner", resume_wake)

    enabled = server._methods["voice.toggle"](
        "worker", {"action": "on", "session_id": "worker-session", "profile": "worker"}
    )
    assert "error" not in enabled
    monkeypatch.setattr(server, "_voice_wake_owner", object())
    config_reads.clear()

    assert server._release_voice_for_transport(transport) is False
    assert cleanup_done.wait(2)
    assert retry_done.wait(2)
    assert attempts == 2
    assert config_reads and set(config_reads) == {worker}
    assert cleanup_homes == [worker]
    assert wake_homes == [worker]
    assert launch not in config_reads + cleanup_homes + wake_homes


def test_same_transport_cannot_control_voice_from_another_profile(monkeypatch, tmp_path):
    _launch, worker = _bind_profiles(monkeypatch, tmp_path)
    transport = object()
    toggles = []
    monkeypatch.setattr(server, "_stdio_transport", transport)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {}})
    monkeypatch.setattr(server, "_set_voice_tts", lambda enabled: toggles.append(enabled))

    enabled = server._methods["voice.toggle"](
        "worker", {"action": "on", "session_id": "shared", "profile": "worker"}
    )
    refused = server._methods["voice.toggle"](
        "launch", {"action": "tts", "session_id": "shared"}
    )

    assert "error" not in enabled
    assert refused["error"]["code"] == 4016
    assert server._voice_authority_transport is transport
    assert server._voice_authority_profile_home == str(worker.resolve())
    assert toggles == []


def test_same_transport_cannot_stop_recording_from_another_profile(monkeypatch, tmp_path):
    _launch, worker = _bind_profiles(monkeypatch, tmp_path)
    transport = object()
    releases = []
    monkeypatch.setattr(server, "_stdio_transport", transport)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"voice": {}})
    monkeypatch.setattr(
        "tools.voice_input.release_voice_input",
        lambda **kwargs: releases.append(kwargs) or True,
    )

    enabled = server._methods["voice.toggle"](
        "worker", {"action": "on", "session_id": "shared", "profile": "worker"}
    )
    refused = server._methods["voice.record"](
        "launch", {"action": "stop", "session_id": "shared"}
    )

    assert "error" not in enabled
    assert refused["error"]["code"] == 4016
    assert server._voice_lease_owner_transport is transport
    assert server._voice_lease_profile_home == str(worker.resolve())
    assert releases == []


def test_same_transport_cannot_stop_wake_from_another_profile(monkeypatch, tmp_path):
    _launch, worker = _bind_profiles(monkeypatch, tmp_path)
    transport = object()
    stopped = []
    monkeypatch.setattr(server, "_stdio_transport", transport)
    monkeypatch.setattr(
        server,
        "_wake_probe",
        lambda _cfg, _params, _surface: (
            "local",
            {"available": True, "phrase": "hey", "provider": "fake", "hint": ""},
        ),
    )
    monkeypatch.setattr("tools.wake_word.load_wake_word_config", lambda: {"enabled": True})
    monkeypatch.setattr("tools.wake_word.wake_surface_enabled", lambda *_args: True)
    monkeypatch.setattr("tools.wake_word.wake_phrase", lambda _cfg: "hey")
    monkeypatch.setattr("tools.wake_word.owns_listener", lambda owner: owner is transport)
    monkeypatch.setattr("tools.wake_word.detector_frame_info", dict)
    monkeypatch.setattr("tools.wake_word.start_listening", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "tools.wake_word.stop_listening",
        lambda **_kwargs: stopped.append(True) or True,
    )

    started = server._methods["wake.start"](
        "worker", {"surface": "tui", "profile": "worker"}
    )
    refused = server._methods["wake.stop"]("launch", {})

    assert started["result"]["started"] is True
    assert refused["result"]["stopped"] is False
    assert refused["result"]["reason"] == "not_owner"
    assert server._wake_owner_transport is transport
    assert server._wake_owner_profile_home == str(worker.resolve())
    assert stopped == []
