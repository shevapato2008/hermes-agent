"""SmartBox local voice adapter — gateway/platforms/smartbox_voice.py.

Covers: adapter construction/config, UDS connect/disconnect lifecycle
(reusing gateway/smartbox_voice_protocol.py's permission hardening), the
ordered-JSONL send path, protocol-frame handling (idempotency dedup, cancel,
resolve_approval), deny-on-disconnect for outstanding challenges, and the
capability-dispatch extension of gateway/run.py's single per-session
``_approval_notify_sync`` callback — including proof that adapters WITHOUT
the new capability still get the legacy send_exec_approval/text-fallback
path byte-for-byte.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.stream_events import ApprovalRequired


def _make_adapter(tmp_path, extra=None):
    from gateway.platforms.smartbox_voice import SmartBoxVoiceAdapter

    sock_path = str(tmp_path / "smartbox.sock")
    cfg_extra = {"socket_path": sock_path}
    cfg_extra.update(extra or {})
    config = PlatformConfig(enabled=True, extra=cfg_extra)
    adapter = SmartBoxVoiceAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter


# ── Construction / config ────────────────────────────────────────────────────

class TestConstruction:
    def test_platform_is_smartbox_voice(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.platform == Platform.SMARTBOX_VOICE

    def test_socket_path_from_config(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.socket_path == str(tmp_path / "smartbox.sock")

    def test_jsonl_path_defaults_relative_to_socket(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.jsonl_path.startswith(str(tmp_path))
        assert adapter.jsonl_path.endswith(".jsonl")

    def test_jsonl_path_overridable(self, tmp_path):
        jsonl = str(tmp_path / "custom-stream.jsonl")
        adapter = _make_adapter(tmp_path, extra={"jsonl_path": jsonl})
        assert adapter.jsonl_path == jsonl


# ── connect() / disconnect() lifecycle ───────────────────────────────────────

class TestConnectDisconnect:
    @pytest.mark.asyncio
    async def test_connect_binds_owner_only_socket(self, tmp_path):
        import stat as stat_mod

        adapter = _make_adapter(tmp_path)
        ok = await adapter.connect()
        try:
            assert ok is True
            assert os.path.exists(adapter.socket_path)
            mode = stat_mod.S_IMODE(os.stat(adapter.socket_path).st_mode)
            assert mode == 0o600
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_removes_socket_file(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        await adapter.connect()
        await adapter.disconnect()
        assert not os.path.exists(adapter.socket_path)

    @pytest.mark.asyncio
    async def test_disconnect_denies_all_outstanding_challenges(self, tmp_path, monkeypatch):
        from tools import approval as approval_mod

        adapter = _make_adapter(tmp_path)
        await adapter.connect()

        calls = []
        monkeypatch.setattr(
            approval_mod, "resolve_gateway_approval",
            lambda session_key, choice, resolve_all=False, challenge_id=None: (
                calls.append((session_key, choice, challenge_id)) or 1
            ),
        )
        adapter._pending_challenges["sess-1"] = {"chal-a", "chal-b"}
        adapter._pending_challenges["sess-2"] = {"chal-c"}

        await adapter.disconnect()

        resolved = {(c[0], c[2]) for c in calls}
        assert resolved == {
            ("sess-1", "chal-a"), ("sess-1", "chal-b"), ("sess-2", "chal-c"),
        }
        assert all(c[1] == "deny" for c in calls)
        assert adapter._pending_challenges == {}


# ── send() → ordered JSONL writer ────────────────────────────────────────────

class TestSend:
    @pytest.mark.asyncio
    async def test_send_writes_ordered_jsonl_record(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        result = await adapter.send("chat-1", "hello there")
        assert result.success is True

        lines = open(adapter.jsonl_path, encoding="utf-8").read().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "message"
        assert record["text"] == "hello there"
        assert record["chat_id"] == "chat-1"
        assert record["seq"] == 0

    @pytest.mark.asyncio
    async def test_send_sequence_increments_per_chat(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        await adapter.send("chat-1", "one")
        await adapter.send("chat-1", "two")
        lines = open(adapter.jsonl_path, encoding="utf-8").read().splitlines()
        seqs = [json.loads(l)["seq"] for l in lines]
        assert seqs == [0, 1]

    @pytest.mark.asyncio
    async def test_get_chat_info_returns_dm_shape(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = await adapter.get_chat_info("chat-1")
        assert info["type"] == "dm"
        assert "name" in info


# ── send_exec_approval_event: the new capability ─────────────────────────────

class TestSendExecApprovalEvent:
    @pytest.mark.asyncio
    async def test_places_typed_object_on_ordered_writer(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        approval = ApprovalRequired(
            challenge_id="chal-1",
            command="rm -rf /tmp/x",
            description="dangerous deletion",
            pattern_keys=["rm_recursive"],
            timeout_seconds=45,
        )
        result = await adapter.send_exec_approval_event(
            chat_id="chat-1", approval=approval, session_key="sess-1",
        )
        assert result.success is True

        lines = open(adapter.jsonl_path, encoding="utf-8").read().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "approval_required"
        assert record["challenge_id"] == "chal-1"
        assert record["command"] == "rm -rf /tmp/x"
        assert record["description"] == "dangerous deletion"
        assert record["pattern_keys"] == ["rm_recursive"]
        assert record["timeout_seconds"] == 45
        assert record["session_key"] == "sess-1"

    @pytest.mark.asyncio
    async def test_tracks_challenge_as_outstanding_for_session(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        approval = ApprovalRequired(
            challenge_id="chal-1", command="ls", description="d",
            pattern_keys=["k"], timeout_seconds=30,
        )
        await adapter.send_exec_approval_event(
            chat_id="chat-1", approval=approval, session_key="sess-1",
        )
        assert "chal-1" in adapter._pending_challenges["sess-1"]

    @pytest.mark.asyncio
    async def test_shares_the_same_writer_as_send(self, tmp_path):
        """Approvals are NOT a second channel/database -- same ordered writer
        as regular message delivery."""
        adapter = _make_adapter(tmp_path)
        await adapter.send("chat-1", "hi")
        approval = ApprovalRequired(
            challenge_id="chal-1", command="ls", description="d",
            pattern_keys=["k"], timeout_seconds=30,
        )
        await adapter.send_exec_approval_event(
            chat_id="chat-1", approval=approval, session_key="sess-1",
        )
        lines = open(adapter.jsonl_path, encoding="utf-8").read().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "message"
        assert json.loads(lines[1])["type"] == "approval_required"


# ── Protocol frame handling ───────────────────────────────────────────────────

class TestUserMessageFrame:
    @pytest.mark.asyncio
    async def test_new_idempotency_key_dispatches_to_handle_message(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        await adapter._handle_user_message_frame(
            {"type": "user_message", "text": "hi", "idempotency_key": "k1"}, writer=None,
        )
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert isinstance(event, MessageEvent)
        assert event.text == "hi"

    @pytest.mark.asyncio
    async def test_no_retry_after_accepted_is_not_redispatched(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        frame = {"type": "user_message", "text": "hi", "idempotency_key": "k1"}
        await adapter._handle_user_message_frame(frame, writer=None)
        await adapter._handle_user_message_frame(frame, writer=None)
        # Only the first call actually dispatched -- the resend after accept
        # must be silently dropped, not reprocessed.
        assert adapter.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_distinct_idempotency_keys_both_dispatch(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        await adapter._handle_user_message_frame(
            {"type": "user_message", "text": "a", "idempotency_key": "k1"}, writer=None,
        )
        await adapter._handle_user_message_frame(
            {"type": "user_message", "text": "b", "idempotency_key": "k2"}, writer=None,
        )
        assert adapter.handle_message.call_count == 2


class TestCancelFrame:
    @pytest.mark.asyncio
    async def test_cancel_sets_interrupt_event_for_session(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        session_key = "sess-1"
        interrupt_event = asyncio.Event()
        adapter._active_sessions[session_key] = interrupt_event

        await adapter._handle_cancel_frame({"type": "cancel"}, session_key)

        assert interrupt_event.is_set()

    @pytest.mark.asyncio
    async def test_cancel_with_no_active_session_is_a_noop(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        await adapter._handle_cancel_frame({"type": "cancel"}, None)  # must not raise


class TestResolveApprovalFrame:
    def test_resolve_approval_calls_registry_with_exact_challenge(self, tmp_path, monkeypatch):
        from tools import approval as approval_mod

        adapter = _make_adapter(tmp_path)
        adapter._pending_challenges["sess-1"] = {"chal-1"}

        calls = []
        monkeypatch.setattr(
            approval_mod, "resolve_gateway_approval",
            lambda session_key, choice, resolve_all=False, challenge_id=None: (
                calls.append((session_key, choice, challenge_id)) or 1
            ),
        )
        adapter._handle_resolve_approval_frame(
            {"type": "resolve_approval", "challenge_id": "chal-1", "choice": "once"},
            "sess-1",
        )
        assert calls == [("sess-1", "once", "chal-1")]
        assert "chal-1" not in adapter._pending_challenges.get("sess-1", set())

    def test_resolve_approval_missing_fields_is_a_noop(self, tmp_path, monkeypatch):
        from tools import approval as approval_mod

        adapter = _make_adapter(tmp_path)
        calls = []
        monkeypatch.setattr(
            approval_mod, "resolve_gateway_approval",
            lambda *a, **kw: calls.append((a, kw)) or 0,
        )
        adapter._handle_resolve_approval_frame({"type": "resolve_approval"}, "sess-1")
        assert calls == []


# ── Capability dispatch: gateway/run.py's extended _approval_notify_sync ────
#
# Exercises the module-level ``_dispatch_exec_approval`` helper that the
# single existing ``_approval_notify_sync`` callback in gateway/run.py calls
# into. Proves BOTH branches: an adapter WITH send_exec_approval_event uses
# it (typed object, ordered wire), and an adapter WITHOUT it falls through to
# the untouched legacy send_exec_approval/text-fallback path.

def _run_loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


class TestDispatchExecApprovalCapability:
    def test_capability_adapter_gets_typed_ordered_wire_delivery(self, tmp_path):
        from gateway.run import _dispatch_exec_approval

        adapter = _make_adapter(tmp_path)
        loop, thread = _run_loop_in_thread()
        try:
            approval_data = {
                "command": "rm -rf /tmp/x",
                "description": "dangerous deletion",
                "pattern_key": "rm_recursive",
                "pattern_keys": ["rm_recursive"],
                "challenge_id": "chal-1",
                "timeout_seconds": 45,
            }
            _dispatch_exec_approval(
                adapter, approval_data,
                chat_id="chat-1", session_key="sess-1", metadata=None, loop=loop,
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

        lines = open(adapter.jsonl_path, encoding="utf-8").read().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["type"] == "approval_required"
        assert record["challenge_id"] == "chal-1"
        assert "chal-1" in adapter._pending_challenges["sess-1"]

    def test_legacy_adapter_without_capability_uses_send_exec_approval(self, tmp_path):
        """An adapter shaped like Telegram/Matrix (has send_exec_approval,
        no send_exec_approval_event) must take the EXACT legacy path."""
        from gateway.run import _dispatch_exec_approval

        class LegacyAdapter:
            def __init__(self):
                self.send_exec_approval_calls = []

            async def send_exec_approval(self, chat_id, command, session_key,
                                          description="dangerous command", metadata=None):
                self.send_exec_approval_calls.append(
                    (chat_id, command, session_key, description, metadata)
                )
                return SendResult(success=True, message_id="42")

        adapter = LegacyAdapter()
        loop, thread = _run_loop_in_thread()
        try:
            approval_data = {
                "command": "rm -rf /tmp/x",
                "description": "dangerous deletion",
                "pattern_key": "rm_recursive",
                "pattern_keys": ["rm_recursive"],
                "challenge_id": "chal-1",
                "timeout_seconds": 45,
            }
            _dispatch_exec_approval(
                adapter, approval_data,
                chat_id="chat-1", session_key="sess-1", metadata=None, loop=loop,
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

        assert len(adapter.send_exec_approval_calls) == 1
        call = adapter.send_exec_approval_calls[0]
        assert call[0] == "chat-1"
        assert call[1] == "rm -rf /tmp/x"
        assert call[2] == "sess-1"
        assert call[3] == "dangerous deletion"

    def test_adapter_with_neither_capability_falls_back_to_text(self, tmp_path):
        """No send_exec_approval_event, no send_exec_approval -- must fall
        all the way through to the plain-text approval prompt via send()."""
        from gateway.run import _dispatch_exec_approval

        class BareAdapter:
            def __init__(self):
                self.sent = []

            async def send(self, chat_id, content, metadata=None):
                self.sent.append((chat_id, content, metadata))
                return SendResult(success=True)

        adapter = BareAdapter()
        loop, thread = _run_loop_in_thread()
        try:
            approval_data = {
                "command": "rm -rf /tmp/x",
                "description": "dangerous deletion",
                "pattern_key": "rm_recursive",
                "pattern_keys": ["rm_recursive"],
                "challenge_id": "chal-1",
                "timeout_seconds": 45,
            }
            _dispatch_exec_approval(
                adapter, approval_data,
                chat_id="chat-1", session_key="sess-1", metadata=None, loop=loop,
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

        assert len(adapter.sent) == 1
        chat_id, content, metadata = adapter.sent[0]
        assert chat_id == "chat-1"
        assert "rm -rf /tmp/x" in content
        assert "/approve" in content
        assert "/deny" in content
