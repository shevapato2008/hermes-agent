"""SmartBox voice adapter — UDS session protocol rules.

These are pure, transport-agnostic state-machine tests for
``gateway/smartbox_voice_protocol.py``: UDS socket permission hardening,
protocol version handshake, idempotency-key bookkeeping (one user message
per key, reconnect-before-accepted retry allowed, no retry-after-accepted),
monotonic delta ordering, the cancel contract, and the ordered JSONL writer
SmartBox reuses for both regular stream delivery and challenge-addressed
approval events.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading

import pytest

from gateway.session import session_revision
from gateway.smartbox_voice_protocol import (
    CancelRequest,
    DeltaSequencer,
    DuplicateMessageError,
    IdempotencyTracker,
    MessageState,
    OrderedJSONLWriter,
    OutOfOrderDeltaError,
    PROTOCOL_VERSION,
    ProtocolVersionError,
    assert_protocol_version,
    assert_uds_permissions,
    bind_unix_socket,
)


# ── UDS permission hardening ─────────────────────────────────────────────────

class TestUdsPermissions:
    def test_bind_unix_socket_creates_owner_only_socket(self, tmp_path):
        sock_path = str(tmp_path / "smartbox.sock")
        sock = bind_unix_socket(sock_path)
        try:
            mode = stat.S_IMODE(os.stat(sock_path).st_mode)
            assert mode == 0o600
        finally:
            sock.close()
            os.unlink(sock_path)

    def test_bind_unix_socket_removes_stale_socket_file(self, tmp_path):
        sock_path = str(tmp_path / "smartbox.sock")
        # Simulate a stale socket file left by an unclean shutdown.
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(sock_path)
        stale.close()

        sock = bind_unix_socket(sock_path)
        try:
            assert os.path.exists(sock_path)
        finally:
            sock.close()
            os.unlink(sock_path)

    def test_assert_uds_permissions_accepts_owner_only(self, tmp_path):
        sock_path = str(tmp_path / "smartbox.sock")
        sock = bind_unix_socket(sock_path)
        try:
            assert_uds_permissions(sock_path)  # must not raise
        finally:
            sock.close()
            os.unlink(sock_path)

    def test_assert_uds_permissions_rejects_group_readable(self, tmp_path):
        sock_path = str(tmp_path / "smartbox.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(sock_path)
        os.chmod(sock_path, 0o660)  # group-readable — insecure
        try:
            with pytest.raises(PermissionError):
                assert_uds_permissions(sock_path)
        finally:
            sock.close()
            os.unlink(sock_path)


# ── Protocol version handshake ───────────────────────────────────────────────

class TestProtocolVersion:
    def test_matching_version_is_accepted(self):
        assert_protocol_version(PROTOCOL_VERSION)  # must not raise

    def test_mismatched_version_is_rejected(self):
        with pytest.raises(ProtocolVersionError):
            assert_protocol_version(PROTOCOL_VERSION + 1)

    def test_version_is_a_stable_positive_int(self):
        assert isinstance(PROTOCOL_VERSION, int)
        assert PROTOCOL_VERSION >= 1


# ── Idempotency: one user message per key ────────────────────────────────────

class TestIdempotencyTracker:
    def test_new_key_is_accepted(self):
        tracker = IdempotencyTracker()
        state = tracker.submit("key-1")
        assert state is MessageState.PENDING

    def test_empty_key_rejected(self):
        tracker = IdempotencyTracker()
        with pytest.raises(ValueError):
            tracker.submit("")

    def test_reconnect_before_accepted_retry_allowed(self):
        """A client that disconnects before the gateway acks its message may
        resend the SAME idempotency key on reconnect — still PENDING, not a
        duplicate."""
        tracker = IdempotencyTracker()
        tracker.submit("key-1")
        # Resubmission before accept() must succeed, not raise.
        state = tracker.submit("key-1")
        assert state is MessageState.PENDING

    def test_no_retry_after_accepted(self):
        """Once accepted, the SAME idempotency key must never be reprocessed —
        this is the 'one user message per idempotency key' guarantee."""
        tracker = IdempotencyTracker()
        tracker.submit("key-1")
        tracker.accept("key-1")
        assert tracker.is_accepted("key-1") is True
        with pytest.raises(DuplicateMessageError):
            tracker.submit("key-1")

    def test_accept_unknown_key_raises(self):
        tracker = IdempotencyTracker()
        with pytest.raises(KeyError):
            tracker.accept("never-submitted")

    def test_distinct_keys_are_independent(self):
        tracker = IdempotencyTracker()
        tracker.submit("key-1")
        tracker.accept("key-1")
        # A different key is unaffected by key-1's accepted state.
        state = tracker.submit("key-2")
        assert state is MessageState.PENDING
        assert tracker.is_accepted("key-2") is False


# ── Delta ordering ────────────────────────────────────────────────────────────

class TestDeltaSequencer:
    def test_next_yields_monotonic_sequence(self):
        seq = DeltaSequencer()
        assert seq.next("turn-1") == 0
        assert seq.next("turn-1") == 1
        assert seq.next("turn-1") == 2

    def test_turns_are_independent(self):
        seq = DeltaSequencer()
        assert seq.next("turn-1") == 0
        assert seq.next("turn-2") == 0
        assert seq.next("turn-1") == 1

    def test_check_accepts_strictly_increasing(self):
        seq = DeltaSequencer()
        seq.check("turn-1", 0)
        seq.check("turn-1", 1)
        seq.check("turn-1", 5)  # gaps are fine, just must increase

    def test_check_rejects_non_increasing(self):
        seq = DeltaSequencer()
        seq.check("turn-1", 3)
        with pytest.raises(OutOfOrderDeltaError):
            seq.check("turn-1", 3)  # repeat
        with pytest.raises(OutOfOrderDeltaError):
            seq.check("turn-1", 2)  # regression

    def test_reset_clears_turn_state(self):
        seq = DeltaSequencer()
        seq.next("turn-1")
        seq.next("turn-1")
        seq.reset("turn-1")
        assert seq.next("turn-1") == 0


# ── Cancel contract ───────────────────────────────────────────────────────────

class TestCancelRequest:
    def test_cancel_request_carries_turn_id(self):
        req = CancelRequest(turn_id="turn-1")
        assert req.turn_id == "turn-1"


# ── session_revision: pure helper, changes only when session id changes ─────

class TestSessionRevision:
    def test_is_deterministic_for_same_inputs(self):
        a = session_revision("agent:main:smartbox_voice:dm:local", "20260101_000000_abcd1234")
        b = session_revision("agent:main:smartbox_voice:dm:local", "20260101_000000_abcd1234")
        assert a == b

    def test_is_sha256_of_key_nul_session_id_truncated_to_24(self):
        import hashlib

        session_key = "agent:main:smartbox_voice:dm:local"
        current_session_id = "20260101_000000_abcd1234"
        expected = hashlib.sha256(
            (session_key + "\x00" + current_session_id).encode("utf-8")
        ).hexdigest()[:24]
        assert session_revision(session_key, current_session_id) == expected

    def test_changes_when_session_id_changes(self):
        session_key = "agent:main:smartbox_voice:dm:local"
        before = session_revision(session_key, "20260101_000000_abcd1234")
        after = session_revision(session_key, "20260101_000000_ffff9999")
        assert before != after

    def test_stable_across_calls_when_session_id_unchanged(self):
        """The whole point: this must NOT depend on any mutable/second store —
        purely a function of (session_key, current_session_id)."""
        session_key = "agent:main:smartbox_voice:dm:local"
        session_id = "20260101_000000_abcd1234"
        results = {session_revision(session_key, session_id) for _ in range(10)}
        assert len(results) == 1

    def test_different_session_keys_produce_different_revisions(self):
        session_id = "20260101_000000_abcd1234"
        a = session_revision("agent:main:smartbox_voice:dm:local-1", session_id)
        b = session_revision("agent:main:smartbox_voice:dm:local-2", session_id)
        assert a != b

    def test_returns_24_char_hex_string(self):
        rev = session_revision("k", "v")
        assert isinstance(rev, str)
        assert len(rev) == 24
        int(rev, 16)  # must be valid hex


# ── Ordered JSONL writer (reused for regular stream + approval events) ──────

class TestOrderedJSONLWriter:
    def test_write_appends_one_json_line(self, tmp_path):
        path = tmp_path / "stream.jsonl"
        writer = OrderedJSONLWriter(path)
        writer.write({"type": "message", "text": "hi", "seq": 0})
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"type": "message", "text": "hi", "seq": 0}

    def test_multiple_writes_preserve_order(self, tmp_path):
        path = tmp_path / "stream.jsonl"
        writer = OrderedJSONLWriter(path)
        for i in range(5):
            writer.write({"seq": i})
        lines = path.read_text().splitlines()
        assert [json.loads(l)["seq"] for l in lines] == [0, 1, 2, 3, 4]

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "stream.jsonl"
        writer = OrderedJSONLWriter(path)
        writer.write({"seq": 0})
        assert path.exists()

    def test_concurrent_writes_do_not_interleave_lines(self, tmp_path):
        path = tmp_path / "stream.jsonl"
        writer = OrderedJSONLWriter(path)

        def _write_many(start):
            for i in range(start, start + 50):
                writer.write({"seq": i})

        threads = [
            threading.Thread(target=_write_many, args=(0,)),
            threading.Thread(target=_write_many, args=(1000,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = path.read_text().splitlines()
        assert len(lines) == 100
        # Every line must be independently valid JSON — no interleaved
        # half-written lines from concurrent writers.
        for line in lines:
            json.loads(line)
