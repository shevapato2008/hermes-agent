"""Tests for challenge-addressed approval resolution alongside legacy FIFO."""

from __future__ import annotations

from contextlib import nullcontext
import os
import threading
import time

from tools import approval as mod
from tools import approval_context, approval_gateway_wait


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)  # health: allow HX013 -- poll background thread queue registration


class TestChallengeAddressedApproval:
    """Challenge-addressed approval resolution, alongside legacy FIFO.

    Covers: timeout default/coercion and canonical settings and the shared deadline value,
    two concurrent approvals resolved independently by challenge_id,
    wrong/replayed challenge rejection, timeout/disconnect deny, and that
    every existing FIFO caller (resolve_gateway_approval with no
    challenge_id) is completely unaffected.
    """

    SESSION_KEY = "test-challenge-session"

    def setup_method(self):
        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod._session_approved.clear()
        mod._permanent_approved.clear()
        mod._pending.clear()

        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "HERMES_GATEWAY_SESSION",
                "HERMES_CRON_SESSION",
                "HERMES_YOLO_MODE",
                "HERMES_SESSION_KEY",
                "HERMES_INTERACTIVE",
            )
        }
        os.environ.pop("HERMES_YOLO_MODE", None)
        os.environ.pop("HERMES_INTERACTIVE", None)
        os.environ.pop("HERMES_CRON_SESSION", None)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        os.environ["HERMES_SESSION_KEY"] = self.SESSION_KEY

    def teardown_method(self):
        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- _ApprovalEntry.challenge_id ------------------------------------

    def test_entry_generates_nonempty_challenge_id_under_128_bytes(self):
        entry = mod._ApprovalEntry({"command": "ls"})
        assert entry.challenge_id
        assert isinstance(entry.challenge_id, str)
        assert len(entry.challenge_id.encode("utf-8")) <= 128

    def test_two_entries_get_distinct_challenge_ids(self):
        a = mod._ApprovalEntry({"command": "ls"})
        b = mod._ApprovalEntry({"command": "pwd"})
        assert a.challenge_id != b.challenge_id

    # -- timeout resolution: default / coercion / canonical settings --------------

    def test_default_timeout_is_300_and_matches_notify_payload(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_config", dict)

        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        def _resolve_soon():
            _wait_until(lambda: bool(mod._gateway_queues.get(self.SESSION_KEY)))
            mod.resolve_gateway_approval(self.SESSION_KEY, "once")

        threading.Thread(target=_resolve_soon).start()
        mod._await_gateway_decision(
            self.SESSION_KEY,
            mod._gateway_notify_cbs[self.SESSION_KEY],
            {"command": "ls", "description": "d", "pattern_key": "k"},
        )
        assert notified["timeout_seconds"] == 300
        assert notified["challenge_id"]

    def test_invalid_timeout_coerces_to_300(self, monkeypatch):
        monkeypatch.setattr(
            approval_context,
            "_get_approval_config",
            lambda: {"timeout": "not-a-number"},
        )
        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        def _resolve_soon():
            _wait_until(lambda: bool(mod._gateway_queues.get(self.SESSION_KEY)))
            mod.resolve_gateway_approval(self.SESSION_KEY, "once")

        threading.Thread(target=_resolve_soon).start()
        mod._await_gateway_decision(
            self.SESSION_KEY,
            mod._gateway_notify_cbs[self.SESSION_KEY],
            {"command": "ls", "description": "d", "pattern_key": "k"},
        )
        assert notified["timeout_seconds"] == 300

    def test_timeout_over_300_preserves_canonical_setting(self, monkeypatch):
        monkeypatch.setattr(
            approval_context,
            "_get_approval_config",
            lambda: {"timeout": 10_000},
        )
        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        def _resolve_soon():
            _wait_until(lambda: bool(mod._gateway_queues.get(self.SESSION_KEY)))
            mod.resolve_gateway_approval(self.SESSION_KEY, "once")

        threading.Thread(target=_resolve_soon).start()
        mod._await_gateway_decision(
            self.SESSION_KEY,
            mod._gateway_notify_cbs[self.SESSION_KEY],
            {"command": "ls", "description": "d", "pattern_key": "k"},
        )
        assert notified["timeout_seconds"] == 10_000

    def test_nonpositive_timeout_matches_expired_deadline(self, monkeypatch):
        monkeypatch.setattr(
            approval_context,
            "_get_approval_config",
            lambda: {"timeout": -5},
        )
        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        result_holder = {}

        def _run():
            result_holder["r"] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": "ls", "description": "d", "pattern_key": "k"},
            )

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=5)
        assert notified["timeout_seconds"] == -5
        assert result_holder["r"]["resolved"] is False

    def test_notify_payload_timeout_equals_actual_wait_deadline(self, monkeypatch):
        """The SAME resolved integer must define both the notify payload's
        timeout_seconds and the real wait deadline -- they can never drift."""
        monkeypatch.setattr(
            approval_context,
            "_get_approval_config",
            lambda: {"timeout": 1},
        )
        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        start = time.monotonic()
        result = mod._await_gateway_decision(
            self.SESSION_KEY,
            mod._gateway_notify_cbs[self.SESSION_KEY],
            {"command": "ls", "description": "d", "pattern_key": "k"},
        )
        elapsed = time.monotonic() - start

        assert notified["timeout_seconds"] == 1
        assert result["resolved"] is False
        assert 0.9 <= elapsed <= 3.0

    def test_notify_payload_preserves_legacy_fields(self, monkeypatch):
        """challenge_id/timeout_seconds are ADDED to a copy -- nothing legacy
        is dropped, and the caller's own dict is never mutated."""
        self._force_fast(monkeypatch)
        original = {
            "command": "rm -rf /tmp/x",
            "description": "dangerous deletion",
            "pattern_key": "rm_recursive",
            "pattern_keys": ["rm_recursive"],
        }
        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        def _resolve_soon():
            _wait_until(lambda: bool(mod._gateway_queues.get(self.SESSION_KEY)))
            mod.resolve_gateway_approval(self.SESSION_KEY, "once")

        threading.Thread(target=_resolve_soon).start()
        mod._await_gateway_decision(
            self.SESSION_KEY, mod._gateway_notify_cbs[self.SESSION_KEY], original
        )
        for key, value in original.items():
            assert notified[key] == value
        assert "challenge_id" not in original
        assert "timeout_seconds" not in original

    def _force_fast(self, monkeypatch, seconds=5):
        monkeypatch.setattr(
            approval_context,
            "_get_approval_config",
            lambda: {"mode": "manual", "timeout": seconds},
        )

    # -- two concurrent approvals, resolved independently by challenge --

    def test_two_concurrent_approvals_resolve_independently_by_challenge(self, monkeypatch):
        self._force_fast(monkeypatch, seconds=10)

        notifications = []
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notifications.append(data)
        )

        results = {}

        def _run(name, command):
            results[name] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": command, "description": "d", "pattern_key": "k"},
            )

        t1 = threading.Thread(target=_run, args=("first", "rm -rf a"))
        t1.start()
        _wait_until(lambda: len(notifications) >= 1)
        t2 = threading.Thread(target=_run, args=("second", "rm -rf b"))
        t2.start()
        _wait_until(lambda: len(notifications) >= 2)

        assert len(notifications) == 2
        assert mod._gateway_queues.get(self.SESSION_KEY) and len(
            mod._gateway_queues[self.SESSION_KEY]
        ) == 2

        first_challenge = notifications[0]["challenge_id"]
        second_challenge = notifications[1]["challenge_id"]
        assert first_challenge != second_challenge

        count = mod.resolve_gateway_approval(
            self.SESSION_KEY, "once", challenge_id=second_challenge
        )
        assert count == 1
        t2.join(timeout=5)
        assert results["second"]["resolved"] is True
        assert results["second"]["choice"] == "once"
        assert "r" not in results.get("first", {})

        count = mod.resolve_gateway_approval(
            self.SESSION_KEY, "deny", challenge_id=first_challenge
        )
        assert count == 1
        t1.join(timeout=5)
        assert results["first"]["resolved"] is True
        assert results["first"]["choice"] == "deny"

    # -- wrong / replayed challenge is rejected --------------------------

    def test_wrong_challenge_id_resolves_nothing(self, monkeypatch):
        self._force_fast(monkeypatch, seconds=2)

        mod.register_gateway_notify(self.SESSION_KEY, lambda data: None)

        result_holder = {}

        def _run():
            result_holder["r"] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": "ls", "description": "d", "pattern_key": "k"},
            )

        t = threading.Thread(target=_run)
        t.start()

        _wait_until(lambda: bool(mod._gateway_queues.get(self.SESSION_KEY)))

        count = mod.resolve_gateway_approval(
            self.SESSION_KEY, "once", challenge_id="not-the-real-challenge"
        )
        assert count == 0
        assert mod._gateway_queues.get(self.SESSION_KEY)

        t.join(timeout=5)
        assert result_holder["r"]["resolved"] is False

    def test_replayed_challenge_id_resolves_nothing_second_time(self, monkeypatch):
        self._force_fast(monkeypatch, seconds=5)

        notified = {}
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notified.update(data)
        )

        result_holder = {}

        def _run():
            result_holder["r"] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": "ls", "description": "d", "pattern_key": "k"},
            )

        t = threading.Thread(target=_run)
        t.start()
        _wait_until(lambda: bool(notified))
        challenge_id = notified["challenge_id"]

        first_count = mod.resolve_gateway_approval(
            self.SESSION_KEY, "once", challenge_id=challenge_id
        )
        assert first_count == 1
        t.join(timeout=5)
        assert result_holder["r"]["choice"] == "once"

        replay_count = mod.resolve_gateway_approval(
            self.SESSION_KEY, "once", challenge_id=challenge_id
        )
        assert replay_count == 0

    # -- timeout / disconnect deny ---------------------------------------

    def test_timeout_produces_deny_shaped_outcome(self, monkeypatch):
        self._force_fast(monkeypatch, seconds=1)
        mod.register_gateway_notify(self.SESSION_KEY, lambda data: None)

        result = mod._await_gateway_decision(
            self.SESSION_KEY,
            mod._gateway_notify_cbs[self.SESSION_KEY],
            {"command": "ls", "description": "d", "pattern_key": "k"},
        )
        assert result["resolved"] is False
        assert result["choice"] is None

    def test_disconnect_via_unregister_denies_pending_challenge(self, monkeypatch):
        """Simulates SmartBox's UDS connection dropping mid-approval."""
        self._force_fast(monkeypatch, seconds=30)
        mod.register_gateway_notify(self.SESSION_KEY, lambda data: None)

        result_holder = {}

        def _run():
            result_holder["r"] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": "ls", "description": "d", "pattern_key": "k"},
            )

        t = threading.Thread(target=_run)
        t.start()
        _wait_until(lambda: bool(mod._gateway_queues.get(self.SESSION_KEY)))

        challenge_id = mod._gateway_queues[self.SESSION_KEY][0].challenge_id
        count = mod.resolve_gateway_approval(
            self.SESSION_KEY, "deny", challenge_id=challenge_id
        )
        assert count == 1
        t.join(timeout=5)
        assert result_holder["r"]["resolved"] is True
        assert result_holder["r"]["choice"] == "deny"

    # -- legacy FIFO callers are completely unaffected --------------------

    def test_legacy_resolve_without_challenge_id_still_fifo(self, monkeypatch):
        self._force_fast(monkeypatch, seconds=10)

        notifications = []
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notifications.append(data)
        )
        results = {}

        def _run(name, command):
            results[name] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": command, "description": "d", "pattern_key": "k"},
            )

        t1 = threading.Thread(target=_run, args=("first", "cmd-a"))
        t1.start()
        _wait_until(lambda: len(notifications) >= 1)
        t2 = threading.Thread(target=_run, args=("second", "cmd-b"))
        t2.start()
        _wait_until(lambda: len(notifications) >= 2)

        count = mod.resolve_gateway_approval(self.SESSION_KEY, "once")
        assert count == 1
        t1.join(timeout=5)
        assert results["first"]["resolved"] is True
        assert results["first"]["choice"] == "once"

        mod.resolve_gateway_approval(self.SESSION_KEY, "deny")
        t2.join(timeout=5)
        assert results["second"]["resolved"] is True
        assert results["second"]["choice"] == "deny"

    def test_resolve_all_still_works_without_challenge_id(self, monkeypatch):
        self._force_fast(monkeypatch, seconds=10)
        notifications = []
        mod.register_gateway_notify(
            self.SESSION_KEY, lambda data: notifications.append(data)
        )
        results = {}

        def _run(name, command):
            results[name] = mod._await_gateway_decision(
                self.SESSION_KEY,
                mod._gateway_notify_cbs[self.SESSION_KEY],
                {"command": command, "description": "d", "pattern_key": "k"},
            )

        threads = [
            threading.Thread(target=_run, args=(f"t{i}", f"cmd-{i}"))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        _wait_until(lambda: len(notifications) >= 3)

        count = mod.resolve_gateway_approval(self.SESSION_KEY, "once", resolve_all=True)
        assert count == 3
        for t in threads:
            t.join(timeout=5)
        for i in range(3):
            assert results[f"t{i}"]["choice"] == "once"


def test_challenge_addressed_denial_preserves_reason(monkeypatch):
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 2)

    def notify(payload):
        assert mod.resolve_gateway_approval(
            "challenge-reason",
            "deny",
            reason="leave the file alone",
            challenge_id=payload["challenge_id"],
        ) == 1

    result = mod._await_gateway_decision("challenge-reason", notify, {"command": "rm x"})
    assert result == {"resolved": True, "choice": "deny", "reason": "leave the file alone"}


def test_challenge_timeout_resolved_once_before_notification(monkeypatch):
    monkeypatch.setattr(approval_gateway_wait, "human_wait_window", lambda _: nullcontext())
    calls = []

    def timeout():
        calls.append(True)
        return 2 if len(calls) == 1 else 999

    monkeypatch.setattr(approval_context, "_get_approval_timeout", timeout)

    def notify(payload):
        assert payload["timeout_seconds"] == 2
        mod.resolve_gateway_approval(
            "challenge-timeout-once", "once", challenge_id=payload["challenge_id"]
        )

    mod._await_gateway_decision("challenge-timeout-once", notify, {"command": "ls"})
    assert calls == [True]
