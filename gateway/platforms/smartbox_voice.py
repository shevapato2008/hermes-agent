"""SmartBox local voice adapter.

A LOCAL platform adapter for the Hermes gateway: the SmartBox voice
assistant app connects over a Unix Domain Socket (see
``gateway/smartbox_voice_protocol.py`` for the wire-level rules) and speaks
a small JSONL-framed protocol. This is the first-party analogue of any other
platform adapter — same ``BasePlatformAdapter`` contract as
Telegram/Discord/etc., same structured stream events
(``gateway/stream_events.py``), same gateway approval registry
(``tools/approval.py``) — just a different transport (UDS instead of a
hosted bot API) and a different rendering surface (the SmartBox device UI
instead of chat bubbles).

Approvals: this adapter implements the ``send_exec_approval_event``
capability (not the legacy ``send_exec_approval``). ``gateway/run.py``'s
single ``_approval_notify_sync`` callback detects that capability and routes
the typed ``ApprovalRequired`` object here instead of the legacy button/text
path — see ``_dispatch_exec_approval`` in ``gateway/run.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import build_session_key
from gateway.smartbox_voice_protocol import (
    DeltaSequencer,
    DuplicateMessageError,
    IdempotencyTracker,
    OrderedJSONLWriter,
    ProtocolVersionError,
    assert_protocol_version,
    assert_uds_permissions,
    bind_unix_socket,
)
from gateway.stream_events import ApprovalRequired

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 8000

# Overridable via PlatformConfig.extra["socket_path"] / SMARTBOX_VOICE_SOCKET_PATH.
DEFAULT_SOCKET_PATH = "/tmp/hermes-smartbox-voice.sock"


def check_smartbox_voice_requirements() -> bool:
    """SmartBox voice has no external dependencies — pure asyncio + stdlib socket."""
    return True


class SmartBoxVoiceAdapter(BasePlatformAdapter):
    """Local UDS adapter for the SmartBox voice assistant app."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    supports_code_blocks = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SMARTBOX_VOICE)
        self.socket_path = str(config.extra.get("socket_path") or DEFAULT_SOCKET_PATH)
        self.jsonl_path = str(config.extra.get("jsonl_path") or f"{self.socket_path}.jsonl")
        self._writer = OrderedJSONLWriter(self.jsonl_path)
        self._delta_seq = DeltaSequencer()
        self._idempotency = IdempotencyTracker()
        self._server: Optional[asyncio.AbstractServer] = None
        self._sock = None
        # session_key -> set of challenge_ids this adapter has sent to the
        # client and not yet resolved. Used to deny-on-disconnect: the
        # SAME gateway approval registry, no second conversation database.
        self._pending_challenges: Dict[str, Set[str]] = {}
        self._chat_id = "smartbox-voice"

    # ------------------------------------------------------------------
    # BasePlatformAdapter lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        try:
            self._sock = bind_unix_socket(self.socket_path)
            assert_uds_permissions(self.socket_path)
        except Exception as e:
            logger.error("SmartBox voice: failed to bind UDS %s: %s", self.socket_path, e)
            return False

        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection, sock=self._sock
            )
        except Exception as e:
            logger.error("SmartBox voice: failed to start UDS server: %s", e)
            return False

        self._running = True
        logger.info("SmartBox voice: listening on %s", self.socket_path)
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

        # The UDS transport is gone -- any challenge still outstanding can
        # never be answered by this client. Deny them all rather than leave
        # the agent thread(s) blocked until the gateway timeout.
        from tools.approval import resolve_gateway_approval
        for session_key, challenge_ids in list(self._pending_challenges.items()):
            for challenge_id in list(challenge_ids):
                try:
                    resolve_gateway_approval(session_key, "deny", challenge_id=challenge_id)
                except Exception:
                    logger.debug(
                        "SmartBox voice: failed to deny outstanding challenge %s on disconnect",
                        challenge_id,
                    )
        self._pending_challenges.clear()

    # ------------------------------------------------------------------
    # Connection / frame handling
    # ------------------------------------------------------------------

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter) -> None:
        session_key: Optional[str] = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                frame_type = frame.get("type")
                if frame_type == "hello":
                    try:
                        assert_protocol_version(int(frame.get("protocol_version", -1)))
                    except (ProtocolVersionError, TypeError, ValueError) as e:
                        await self._write_frame(writer, {"type": "error", "message": str(e)})
                        break
                elif frame_type == "user_message":
                    new_session_key = await self._handle_user_message_frame(frame, writer)
                    if new_session_key:
                        session_key = new_session_key
                elif frame_type == "cancel":
                    await self._handle_cancel_frame(frame, session_key)
                elif frame_type == "resolve_approval":
                    self._handle_resolve_approval_frame(frame, session_key)
        finally:
            try:
                if writer is not None and not writer.is_closing():
                    writer.close()
            except Exception:
                pass
            # Connection lost mid-approval -- deny anything still
            # outstanding for this session. Drives the SAME registry;
            # never a second approval channel.
            if session_key:
                from tools.approval import resolve_gateway_approval
                for challenge_id in list(self._pending_challenges.get(session_key, set())):
                    try:
                        resolve_gateway_approval(session_key, "deny", challenge_id=challenge_id)
                    except Exception:
                        logger.debug(
                            "SmartBox voice: failed to deny challenge %s on connection close",
                            challenge_id,
                        )
                self._pending_challenges.pop(session_key, None)

    @staticmethod
    async def _write_frame(writer: asyncio.StreamWriter, frame: dict) -> None:
        writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()

    async def _handle_user_message_frame(self, frame: dict,
                                          writer: Optional[asyncio.StreamWriter]) -> Optional[str]:
        """Handle an inbound ``user_message`` frame.

        Idempotency: a brand-new key or a still-PENDING key (reconnect
        before this frame's accept) dispatches; a key already ACCEPTED is a
        no-retry-after-accepted violation and is silently dropped — the
        earlier accepted copy is authoritative, so re-dispatching would
        violate "one user message per idempotency key".
        """
        idempotency_key = str(frame.get("idempotency_key") or "")
        try:
            self._idempotency.submit(idempotency_key)
        except (DuplicateMessageError, ValueError):
            return None

        source = self.build_source(
            chat_id=self._chat_id,
            chat_name="SmartBox",
            chat_type="dm",
            user_id="smartbox-local",
            message_id=idempotency_key or None,
        )
        event = MessageEvent(
            text=str(frame.get("text", "")),
            message_type=MessageType.TEXT,
            source=source,
            message_id=idempotency_key or None,
        )
        session_key = build_session_key(source)

        # Accept BEFORE dispatch: once handed to the gateway, this key is
        # done — a later resend (even from this same call re-entering
        # somehow) must never be reprocessed.
        self._idempotency.accept(idempotency_key)

        task = asyncio.ensure_future(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return session_key

    async def _handle_cancel_frame(self, frame: dict, session_key: Optional[str]) -> None:
        if not session_key:
            return
        interrupt_event = self._active_sessions.get(session_key)
        if interrupt_event is not None:
            interrupt_event.set()

    def _handle_resolve_approval_frame(self, frame: dict, session_key: Optional[str]) -> None:
        """Resolve a challenge-addressed approval. Always requires the exact
        challenge — there is no FIFO fallback for SmartBox frames."""
        challenge_id = frame.get("challenge_id")
        choice = frame.get("choice")
        if not challenge_id or not choice or not session_key:
            return
        from tools.approval import resolve_gateway_approval
        resolve_gateway_approval(session_key, choice, challenge_id=challenge_id)
        self._pending_challenges.get(session_key, set()).discard(challenge_id)

    # ------------------------------------------------------------------
    # BasePlatformAdapter required methods
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        seq = self._delta_seq.next(chat_id)
        self._writer.write({
            "type": "message",
            "chat_id": chat_id,
            "text": content,
            "reply_to": reply_to,
            "seq": seq,
            "ts": time.time(),
        })
        return SendResult(success=True, message_id=str(seq))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # SmartBox renders its own local "listening/thinking" affordance;
        # no separate typing frame needed on the wire.
        pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "SmartBox", "type": "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Challenge-addressed approval capability
    # ------------------------------------------------------------------

    async def send_exec_approval_event(
        self,
        chat_id: str,
        approval: ApprovalRequired,
        session_key: str,
        metadata: Optional[dict] = None,
    ) -> SendResult:
        """Place the typed ``ApprovalRequired`` object on the ordered JSONL writer.

        This is the capability ``gateway/run.py``'s ``_dispatch_exec_approval``
        detects via ``getattr(type(adapter), "send_exec_approval_event", None)``
        and schedules INSTEAD of the legacy ``send_exec_approval``/text
        fallback. Uses the SAME ``OrderedJSONLWriter`` as :meth:`send` — not
        a second channel.
        """
        self._pending_challenges.setdefault(session_key, set()).add(approval.challenge_id)
        seq = self._delta_seq.next(chat_id)
        self._writer.write({
            "type": "approval_required",
            "chat_id": chat_id,
            "session_key": session_key,
            "challenge_id": approval.challenge_id,
            "command": approval.command,
            "description": approval.description,
            "pattern_keys": list(approval.pattern_keys),
            "timeout_seconds": approval.timeout_seconds,
            "seq": seq,
            "ts": time.time(),
        })
        return SendResult(success=True, message_id=approval.challenge_id)
