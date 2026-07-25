"""SmartBox local voice adapter — UDS session protocol.

Defines the pure, transport-agnostic protocol rules used by
``gateway/platforms/smartbox_voice.py``:

  * UDS socket permission hardening (owner-only, 0600) — mirrors the
    existing Unix-domain-socket precedent in
    ``tools/code_execution_tool.py``'s local RPC server.
  * A protocol version handshake constant + compatibility check.
  * Idempotency-key bookkeeping so at most one user message per key is ever
    *accepted* by the gateway, while still allowing a client to retry a
    message that was interrupted before the gateway acknowledged it
    (reconnect-before-accepted). Once accepted, the same key can never be
    resubmitted (no-retry-after-accepted).
  * A monotonic per-turn delta-sequence enforcer for the ordered outbound
    stream.
  * A cancel-frame contract.
  * ``OrderedJSONLWriter`` — the single append-only, strictly-ordered JSONL
    sink that the adapter uses for BOTH its regular stream-event delivery
    and challenge-addressed approval events (``ApprovalRequired``). There is
    intentionally only one writer/channel — approvals are not a second
    conversation database, just another record type on the same wire.

Kept deliberately free of asyncio/socket I/O beyond the UDS bind helper, so
the state-machine rules above are unit-testable without a real client
connection; ``gateway/platforms/smartbox_voice.py`` wires this to a real
``asyncio.start_unix_server`` listener.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict


# ---------------------------------------------------------------------------
# UDS permission hardening
# ---------------------------------------------------------------------------

def bind_unix_socket(sock_path: str) -> socket.socket:
    """Bind and harden a Unix Domain Socket for the SmartBox local protocol.

    Owner-only (0600) — mirrors the existing UDS precedent in
    ``tools/code_execution_tool.py``'s local RPC server. Removes a stale
    socket file left behind by a previous unclean shutdown before binding.
    """
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(sock_path)
        os.chmod(sock_path, 0o600)
        sock.listen(1)
    except BaseException:
        sock.close()
        raise
    return sock


def assert_uds_permissions(sock_path: str) -> None:
    """Raise ``PermissionError`` if *sock_path* is not owner-only (0600).

    Defense in depth beyond the ``os.chmod`` in :func:`bind_unix_socket` —
    guards against a umask race or an operator manually loosening
    permissions on the socket file after bind.
    """
    mode = stat.S_IMODE(os.stat(sock_path).st_mode)
    if mode != 0o600:
        raise PermissionError(
            f"SmartBox voice socket {sock_path!r} has insecure permissions "
            f"{oct(mode)}; expected 0o600 (owner read/write only)"
        )


# ---------------------------------------------------------------------------
# Protocol version handshake
# ---------------------------------------------------------------------------

# Bump on any wire-incompatible change to frame shapes below. A mismatched
# client is rejected at handshake rather than allowed to send frames the
# gateway may misinterpret.
PROTOCOL_VERSION = 1


class ProtocolVersionError(Exception):
    """Raised when a connecting client's protocol version is unsupported."""


def assert_protocol_version(peer_version: int) -> None:
    """Raise :class:`ProtocolVersionError` unless *peer_version* matches."""
    if peer_version != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"unsupported SmartBox voice protocol version {peer_version!r}; "
            f"gateway supports {PROTOCOL_VERSION}"
        )


# ---------------------------------------------------------------------------
# Idempotency: one user message per idempotency key
# ---------------------------------------------------------------------------

class MessageState(Enum):
    """Lifecycle of one inbound user-message idempotency key."""

    PENDING = "pending"    # seen by the gateway, not yet accepted for processing
    ACCEPTED = "accepted"  # gateway has taken ownership — no further retries allowed


class DuplicateMessageError(Exception):
    """Raised when an already-ACCEPTED idempotency key is resubmitted."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            f"idempotency key {idempotency_key!r} was already accepted; "
            "retry-after-accepted is not allowed"
        )
        self.idempotency_key = idempotency_key


class IdempotencyTracker:
    """Tracks idempotency keys for inbound ``user_message`` frames.

    Rules:
      * A brand-new key may always be submitted -> becomes ``PENDING``.
      * A key already ``PENDING`` may be resubmitted without error — this is
        the "reconnect-before-accepted retry" case (the client disconnected
        before it received the gateway's accept acknowledgement and is
        retrying the same logical message) — and stays ``PENDING``.
      * A key already ``ACCEPTED`` must never be resubmitted — "no retry
        after accepted" — :meth:`submit` raises :class:`DuplicateMessageError`.

    Pure in-memory state; not persisted (no second conversation database —
    this only exists to make the idempotency contract correct for the
    lifetime of one gateway process / session).
    """

    def __init__(self) -> None:
        self._states: Dict[str, MessageState] = {}

    def submit(self, idempotency_key: str) -> MessageState:
        if not idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        state = self._states.get(idempotency_key)
        if state is MessageState.ACCEPTED:
            raise DuplicateMessageError(idempotency_key)
        self._states[idempotency_key] = MessageState.PENDING
        return MessageState.PENDING

    def accept(self, idempotency_key: str) -> None:
        if idempotency_key not in self._states:
            raise KeyError(idempotency_key)
        self._states[idempotency_key] = MessageState.ACCEPTED

    def is_accepted(self, idempotency_key: str) -> bool:
        return self._states.get(idempotency_key) is MessageState.ACCEPTED


# ---------------------------------------------------------------------------
# Delta ordering
# ---------------------------------------------------------------------------

class OutOfOrderDeltaError(Exception):
    """Raised when a delta's sequence number does not strictly increase."""


class DeltaSequencer:
    """Enforces strictly increasing delta sequence numbers within one turn.

    Two modes of use:
      * :meth:`next` — generate the next sequence number for an outbound
        record (adapter is the sole writer, so this can never go out of order
        by construction).
      * :meth:`check` — validate an externally-supplied sequence number
        (e.g. re-validating what was just written, or checking an inbound
        claim), raising :class:`OutOfOrderDeltaError` on a repeat or
        regression.
    """

    def __init__(self) -> None:
        self._last: Dict[str, int] = {}

    def next(self, turn_id: str) -> int:
        n = self._last.get(turn_id, -1) + 1
        self._last[turn_id] = n
        return n

    def check(self, turn_id: str, seq: int) -> None:
        last = self._last.get(turn_id)
        if last is not None and seq <= last:
            raise OutOfOrderDeltaError(
                f"turn {turn_id!r}: sequence {seq} is not greater than last seen {last}"
            )
        self._last[turn_id] = seq

    def reset(self, turn_id: str) -> None:
        self._last.pop(turn_id, None)


# ---------------------------------------------------------------------------
# Cancel contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CancelRequest:
    """An inbound ``cancel`` frame — abort the in-flight turn for ``turn_id``."""

    turn_id: str


# ---------------------------------------------------------------------------
# Ordered JSONL writer
# ---------------------------------------------------------------------------

class OrderedJSONLWriter:
    """Append-only, strictly-ordered JSONL writer for one SmartBox session stream.

    Line-buffered text appends only — no read-modify-write — so concurrent
    readers only ever observe whole lines, and concurrent writers (guarded by
    an internal lock) never interleave partial lines. This is the single
    outbound channel the adapter uses for both regular stream events
    (``message``, tool chrome, …) and challenge-addressed approval events
    (``approval_required``) — deliberately not a second, approval-specific
    channel or database.
    """

    def __init__(self, path: "str | Path") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
