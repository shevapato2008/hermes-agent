"""Profile isolation for the ambient thinking-sound worker."""

import threading


def test_thinking_sound_worker_keeps_the_callers_profile(monkeypatch, tmp_path):
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from tools import voice_mode

    launch = tmp_path / "launch"
    worker = tmp_path / "worker"
    launch.mkdir()
    worker.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    seen = []
    ran = threading.Event()

    def fake_loop(_stop, _should_play):
        seen.append(get_hermes_home())
        ran.set()

    voice_mode.stop_thinking_sound()
    monkeypatch.setattr(voice_mode, "thinking_sound_enabled", lambda: True)
    monkeypatch.setattr(voice_mode, "_thinking_sound_loop", fake_loop)
    token = set_hermes_home_override(worker)
    try:
        assert voice_mode.start_thinking_sound() is True
    finally:
        reset_hermes_home_override(token)

    assert ran.wait(2)
    voice_mode.stop_thinking_sound()
    assert seen == [worker]
    assert launch not in seen
