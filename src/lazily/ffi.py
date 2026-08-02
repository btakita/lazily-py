"""C-ABI FFI boundary — the wire-side status/kind discriminants and a
canonical-bytes encode/decode path.

The Python counterpart of the ``lazily-spec`` FFI types
(``lazily-spec/schemas/ffi.json``). ``ffi = host`` for CPython (a C-ABI is
available), so a conforming binding exposes:

- :class:`LazilyFfiStatus` — the operation status code (0..5);
- :class:`LazilyFfiMessageKind` — the IPC message kind discriminator, **including
  ``CrdtSync = 3``** (the spec mandates the FFI kind discriminant MUST carry
  CrdtSync);
- :class:`LazilyFfiBytes` — an owned byte buffer crossing the boundary
  (``ctypes`` structure with ``ptr``/``len``);
- :func:`encode_message` / :func:`decode_message` — re-encode an
  :class:`~lazily.ipc.IpcMessage` to canonical JSON bytes and recover
  ``(status, kind, bytes)``; decode bytes back to an ``IpcMessage``.

The byte payload is the same externally-tagged JSON (``u8`` arrays, PascalCase)
that the IPC wire protocol defines, so a re-encoded message is byte-compatible
with the Rust/Zig FFI boundaries.
"""

from __future__ import annotations


__all__ = [
    "FFI_HEADER_LEN",
    "LazilyFfiBytes",
    "LazilyFfiMessageKind",
    "LazilyFfiStatus",
    "decode_message",
    "encode_message",
    "kind_of",
]

import ctypes
import json
from enum import IntEnum

from .ipc import IpcMessage


class LazilyFfiStatus(IntEnum):
    """FFI operation status code (``ffi.json`` ``LazilyFfiStatus``)."""

    Ok = 0
    Empty = 1
    NullPointer = 2
    InvalidMessage = 3
    EncodeFailed = 4
    Panic = 5


class LazilyFfiMessageKind(IntEnum):
    """IPC message kind discriminator (``ffi.json`` ``LazilyFfiMessageKind``).

    The spec mandates the FFI kind discriminant MUST include ``CrdtSync = 3``.
    """

    Unknown = 0
    Snapshot = 1
    Delta = 2
    CrdtSync = 3
    ResyncRequest = 4
    OutboxAck = 5


# A fixed-length header prefix for an FFI byte buffer: a small, stable preamble
# carrying the kind + status discriminants alongside the payload length, so a
# receiver can dispatch without re-parsing the JSON. Mirrors the Rust
# ``LazilyFfiBytes`` header layout (kind, status, len).
FFI_HEADER_LEN = 16


class LazilyFfiBytes(ctypes.Structure):
    """Owned byte buffer crossing the FFI boundary (``ffi.json``
    ``LazilyFfiBytes``): a ``ptr``/``len`` pair.

    When populated via :func:`encode_message`, ``ptr`` is a ``c_char_p`` into a
    retained bytes object kept alive on the Python side; ``len`` is the payload
    length in bytes. Callers crossing into C must keep the owning Python bytes
    object alive for the lifetime of the pointer.
    """

    _fields_ = [
        ("ptr", ctypes.c_char_p),
        ("len", ctypes.c_size_t),
    ]

    def __init__(self, *, ptr: int = 0, length: int = 0) -> None:
        super().__init__()
        self.ptr = ctypes.c_char_p(ptr)
        self.len = ctypes.c_size_t(length)


def kind_of(message: IpcMessage) -> LazilyFfiMessageKind:
    """The FFI message-kind discriminator for an :class:`IpcMessage`.

    **Deliberate leniency.** An envelope tag this build has no arm for returns
    :attr:`LazilyFfiMessageKind.Unknown` rather than raising, because this
    function is called on the C ABI side of the boundary where a Python
    exception cannot cross. ``Unknown`` is not a silent default: it is a
    *reported* one — :func:`encode_message` refuses any message whose kind is
    ``Unknown`` with :attr:`LazilyFfiStatus.InvalidMessage` and an empty
    payload, so the leniency here converts an unrepresentable value into a
    status code instead of an unwinding exception, and never into a
    misclassified message.

    The ``to_wire()`` call is guarded because it is itself fail-closed: an
    ``IpcMessage`` with every variant slot unset raises ``ValueError`` there,
    which made the ``Unknown`` return below unreachable and the
    ``InvalidMessage`` guard in :func:`encode_message` dead code — so
    ``encode_message`` on such a message raised straight through the boundary it
    documents as never raising. Catching it here is what makes the documented
    status path real.

    Pinned by ``tests/test_library_leniency.py``.
    """
    try:
        wire = message.to_wire()
    except ValueError:
        return LazilyFfiMessageKind.Unknown
    if "Snapshot" in wire:
        return LazilyFfiMessageKind.Snapshot
    if "Delta" in wire:
        return LazilyFfiMessageKind.Delta
    if "CrdtSync" in wire:
        return LazilyFfiMessageKind.CrdtSync
    if "ResyncRequest" in wire:
        return LazilyFfiMessageKind.ResyncRequest
    if "OutboxAck" in wire:
        return LazilyFfiMessageKind.OutboxAck
    return LazilyFfiMessageKind.Unknown


def encode_message(
    message: IpcMessage,
) -> tuple[LazilyFfiStatus, LazilyFfiMessageKind, bytes]:
    """Encode an :class:`~lazily.ipc.IpcMessage` to canonical JSON bytes and
    return ``(status, kind, payload_bytes)``.

    The payload is the same externally-tagged JSON the IPC wire protocol defines
    (``IpcMessage.encode_json``), so a re-encoded message is byte-compatible
    with the Rust/Zig FFI boundaries. ``status`` is :attr:`LazilyFfiStatus.Ok`
    on success, or :attr:`LazilyFfiStatus.EncodeFailed` on a serialization
    failure (never raises across the boundary).
    """
    kind = kind_of(message)
    if kind is LazilyFfiMessageKind.Unknown:
        return LazilyFfiStatus.InvalidMessage, kind, b""
    try:
        payload = message.encode_json()
    except (TypeError, ValueError):
        return LazilyFfiStatus.EncodeFailed, kind, b""
    return LazilyFfiStatus.Ok, kind, payload


def decode_message(payload: bytes) -> tuple[LazilyFfiStatus, IpcMessage | None]:
    """Decode canonical JSON bytes back to an :class:`IpcMessage` and return
    ``(status, message)``. ``status`` is :attr:`LazilyFfiStatus.Ok` on success,
    :attr:`LazilyFfiStatus.Empty` if the payload is empty, or
    :attr:`LazilyFfiStatus.InvalidMessage` on a parse failure (never raises
    across the boundary).

    **Deliberate leniency.** The three swallowed exception types are exactly the
    ways :meth:`IpcMessage.decode_json` reports a bad frame — ``ValueError`` for
    malformed JSON *and* for an unknown envelope tag, ``KeyError`` for a missing
    required field, ``TypeError`` for a field of the wrong shape. They are
    swallowed because the FFI contract is that no Python exception unwinds
    through the C ABI into a Rust/Zig caller, which would abort the host
    process. Nothing is defaulted: the message is ``None`` and the caller gets
    :attr:`LazilyFfiStatus.InvalidMessage`, which is a rejection, not an
    acceptance. Pinned by ``tests/test_library_leniency.py``.
    """
    if not payload:
        return LazilyFfiStatus.Empty, None
    try:
        return LazilyFfiStatus.Ok, IpcMessage.decode_json(payload)
    except (ValueError, KeyError, TypeError):
        return LazilyFfiStatus.InvalidMessage, None


def ffi_bytes_of(payload: bytes) -> tuple[LazilyFfiBytes, object]:
    """Wrap a payload into a :class:`LazilyFfiBytes` (``ptr``/``len``) and
    return ``(ffi_bytes, owning_bytes)``. The caller MUST keep ``owning_bytes``
    alive for the lifetime of the pointer."""
    owning = bytes(payload)
    buf = LazilyFfiBytes()
    buf.ptr = ctypes.c_char_p(owning)
    buf.len = ctypes.c_size_t(len(owning))
    return buf, owning


def _wire_kind_of(message: IpcMessage) -> LazilyFfiMessageKind:  # pragma: no cover
    """Backwards-compatible alias retained for callers that named the helper
    directly. Prefer :func:`kind_of`."""
    return kind_of(message)


def _canonical_dump(message: IpcMessage) -> str:  # pragma: no cover
    """Debug helper: the canonical JSON text of a message (sorted, no spaces)."""
    return json.dumps(message.to_wire(), sort_keys=True, separators=(",", ":"))
