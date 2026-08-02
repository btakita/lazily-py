"""lazily IPC wire protocol — Python binding.

Transport-agnostic Snapshot / Delta state image for ``lazily-ipc``, matching the
normative wire format defined by ``lazily-spec`` (``protocol.md`` + the canonical
``conformance/`` fixtures).

The JSON representation produced here is byte-compatible with the Rust reference
(`lazily-rs`) and the Zig binding: enums are **externally tagged**
(``{"Snapshot": {...}}``, ``{"Payload": [..]}``), wire-stable identifiers
(:data:`NodeId` / :data:`PeerId`) are bare integers, and serialized value bytes
are JSON arrays of ``u8`` rather than base64.

This module deliberately does not know whether messages travel over a unix
socket, pipe, WebSocket, WebRTC data channel, or shared-memory ring buffer. It
defines the stable serializable state plane and the permission-filtered
construction helpers that any transport can carry.
"""

from __future__ import annotations


__all__ = [
    "NODE_KEY_MAX_LEN",
    "NODE_KEY_MAX_SEGMENTS",
    "PROTOCOL_ID",
    "PROTOCOL_MAJOR_VERSION",
    "SHM_BLOB_HEADER_LEN",
    "BlobBackendKind",
    "CapabilityHandshake",
    "CausalReceipt",
    "CausalReceipts",
    "CrdtOp",
    "CrdtSync",
    "Delta",
    "DeltaApplyStatus",
    "DeltaApplyStatusKind",
    "DeltaOp",
    "DeltaOp_CellSet",
    "DeltaOp_EdgeAdd",
    "DeltaOp_EdgeRemove",
    "DeltaOp_Invalidate",
    "DeltaOp_NodeAdd",
    "DeltaOp_NodeRemove",
    "DeltaOp_SlotValue",
    "EdgeSnapshot",
    "IpcMessage",
    "IpcValue",
    "IpcValue_Inline",
    "IpcValue_SharedBlob",
    "NodeId",
    "NodeKey",
    "NodeKeyError",
    "NodeSnapshot",
    "NodeState",
    "NodeState_Opaque",
    "NodeState_Payload",
    "NodeState_SharedBlob",
    "OpKind",
    "OutboxAck",
    "PeerId",
    "PeerPermissions",
    "PermissionDenied",
    "ReceiptApplyResult",
    "ReceiptOutcome",
    "ReceiptProjection",
    "RemoteOp",
    "ResyncRequest",
    "ShmBlobArena",
    "ShmBlobArenaError",
    "ShmBlobCapacityTooSmall",
    "ShmBlobChecksumMismatch",
    "ShmBlobDescriptorMismatch",
    "ShmBlobDescriptorOutOfBounds",
    "ShmBlobGenerationOverflow",
    "ShmBlobRef",
    "ShmBlobTooLarge",
    "Snapshot",
    "WireStamp",
]

import json
import struct
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from .msgpack_codec import msgpack_pack, msgpack_unpack


if TYPE_CHECKING:
    from collections.abc import Iterable


# ---------------------------------------------------------------------------
# Wire-stable identifiers
# ---------------------------------------------------------------------------

#: Wire-stable identifier for a reactive node (cell or slot). Serialized as a
#: bare JSON number. JavaScript/TypeScript peers must keep values at or below
#: ``Number.MAX_SAFE_INTEGER`` (2**53).
NodeId = int

#: Identifies a remote peer participating in a distributed session.
PeerId = int


def _bytes_to_wire(data: bytes) -> list[int]:
    return list(data)


def _bytes_from_wire(value: Any) -> bytes:
    return bytes(value)


# ---------------------------------------------------------------------------
# NodeKey (optional wire-stable keyed address)
# ---------------------------------------------------------------------------


#: Maximum encoded byte length of a :class:`NodeKey` path.
NODE_KEY_MAX_LEN = 1024
#: Maximum number of ``/``-separated segments in a :class:`NodeKey`.
NODE_KEY_MAX_SEGMENTS = 32


class NodeKeyError(ValueError):
    """Why a :class:`NodeKey` path failed validation.

    Mirrors the ``lazily-rs`` ``NodeKeyError`` enum: a ``kind`` discriminator
    (one of the class constants below) plus context describing the offending
    path. Bounds are checked on construction and on the wire.
    """

    #: The path was empty.
    EMPTY = "empty"
    #: The path exceeded :data:`NODE_KEY_MAX_LEN` bytes.
    TOO_LONG = "too_long"
    #: The path had more than :data:`NODE_KEY_MAX_SEGMENTS` segments.
    TOO_MANY_SEGMENTS = "too_many_segments"
    #: The path contained an empty segment (leading/trailing/double ``/``).
    EMPTY_SEGMENT = "empty_segment"

    def __init__(
        self,
        kind: str,
        *,
        length: int | None = None,
        segments: int | None = None,
    ) -> None:
        self.kind = kind
        self.length = length
        self.segments = segments
        super().__init__(self._message())

    def _message(self) -> str:
        if self.kind is NodeKeyError.EMPTY:
            return "node key path is empty"
        if self.kind is NodeKeyError.TOO_LONG:
            return f"node key path is {self.length} bytes, exceeds {NODE_KEY_MAX_LEN}"
        if self.kind is NodeKeyError.TOO_MANY_SEGMENTS:
            return (
                f"node key has {self.segments} segments, "
                f"exceeds {NODE_KEY_MAX_SEGMENTS}"
            )
        return "node key path has an empty segment"


@dataclass(frozen=True, slots=True)
class NodeKey:
    """Wire-stable keyed address for a collection entry.

    A ``/``-joined path (e.g. ``scores/alice``, ``outer/k1/inner/k2``). Unlike
    :data:`NodeId` — the volatile internal handle a producer may re-mint after a
    resync or remove-then-readd — a :class:`NodeKey` is producer-defined and
    **stable across NodeId churn**, so a peer can subscribe to "entry
    ``scores/alice``" without an out-of-band key→NodeId map. A multi-segment
    path addresses nested collections with no extra machinery.

    :class:`NodeKey` is **additive**: it never changes :data:`NodeId` semantics.
    It appears only as the optional ``key`` field on :class:`NodeSnapshot` and
    :class:`DeltaOp_NodeAdd`. Length and segment count are bounded
    (:data:`NODE_KEY_MAX_LEN`, :data:`NODE_KEY_MAX_SEGMENTS`) to cap
    attacker-controlled growth; oversized keys are rejected on construction and
    on the wire.

    On the wire :class:`NodeKey` serializes as a bare JSON string, and a missing
    ``key`` field decodes to ``None`` (``null``) so pre-``key`` encoders and the
    existing conformance fixtures round-trip unchanged.
    """

    path: str

    @classmethod
    def new(cls, path: str) -> NodeKey:
        """Construct a validated key from a ``/``-joined path."""
        cls._validate(path)
        return cls(path)

    @classmethod
    def from_segments(cls, segments: Iterable[str]) -> NodeKey:
        """Construct a key from segments joined with ``/`` (then validated)."""
        return cls.new("/".join(segments))

    @staticmethod
    def _validate(path: str) -> None:
        if not path:
            raise NodeKeyError(NodeKeyError.EMPTY)
        byte_len = len(path.encode("utf-8"))
        if byte_len > NODE_KEY_MAX_LEN:
            raise NodeKeyError(NodeKeyError.TOO_LONG, length=byte_len)
        parts = path.split("/")
        if any(segment == "" for segment in parts):
            raise NodeKeyError(NodeKeyError.EMPTY_SEGMENT)
        if len(parts) > NODE_KEY_MAX_SEGMENTS:
            raise NodeKeyError(NodeKeyError.TOO_MANY_SEGMENTS, segments=len(parts))

    def as_str(self) -> str:
        """The full ``/``-joined path."""
        return self.path

    def segments(self) -> list[str]:
        """The path segments."""
        return self.path.split("/")

    def __str__(self) -> str:
        return self.path

    def to_wire(self) -> str:
        """Serialize as a bare JSON string (matches ``serde_str``)."""
        return self.path

    @classmethod
    def from_wire(cls, value: str) -> NodeKey:
        """Deserialize and validate a bare string path."""
        return cls.new(value)


# ---------------------------------------------------------------------------
# Blob backend discriminator (zero-copy transport)
# ---------------------------------------------------------------------------


class BlobBackendKind(Enum):
    """Which pluggable blob backend holds a :class:`ShmBlobRef` descriptor's bytes.

    The receiver routes descriptor resolution by this discriminator (a ``shm``
    descriptor never resolves in an Arrow table and vice versa). Mirrors the
    ``lazily-rs`` ``BlobBackendKind`` enum and the ``backend`` field of the
    ``ShmBlobRef`` schema (``lazily-spec/schemas/defs.json``,
    ``docs/zero-copy-transport.md``, ``#lzzcpy``).
    """

    #: POSIX shared-memory region (``shm_open`` + ``mmap``) — the default
    #: cross-process backend (same host).
    SHM = "shm"
    #: Apache Arrow IPC stream / Flight-resolved buffer — columnar zero-copy.
    ARROW = "arrow"
    #: An in-process arena (single address space — the FFI host / an editor
    #: plugin loaded in the same process).
    IN_PROCESS = "in_process"

    @classmethod
    def from_wire(cls, value: str) -> BlobBackendKind:
        """Parse a backend discriminator from its wire string.

        Unknown strings fall back to :attr:`SHM` (the default) so a legacy or
        forward-compatible descriptor never hard-fails resolution.
        """
        try:
            return cls(value)
        except ValueError:
            return cls.SHM

    def is_default(self) -> bool:
        """Whether this is the default backend (:attr:`SHM`).

        Used to omit the field on the wire so legacy descriptors round-trip.
        """
        return self is BlobBackendKind.SHM


# ---------------------------------------------------------------------------
# Shared-memory blob descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShmBlobRef:
    """Descriptor for a payload stored in a blob backend (zero-copy transport).

    The standard fields locate and integrity-check a byte range within the
    backend's resolved buffer; :attr:`backend` selects which pluggable backend
    resolves it. ``backend`` is optional and defaults to
    :attr:`BlobBackendKind.SHM`, so every legacy descriptor validates unchanged —
    the transport is a strict superset of the pre-existing shared-memory blob
    path (see ``docs/zero-copy-transport.md``, ``#lzzcpy``).

    The arena header itself is backend-agnostic and does not store ``backend`` —
    the discriminator is wire-level routing, not arena storage.
    """

    offset: int
    len: int
    generation: int
    epoch: int
    checksum: int
    backend: BlobBackendKind = BlobBackendKind.SHM

    def to_wire(self) -> dict[str, int | str]:
        wire: dict[str, int | str] = {
            "offset": self.offset,
            "len": self.len,
            "generation": self.generation,
            "epoch": self.epoch,
            "checksum": self.checksum,
        }
        # Omit the default backend so legacy descriptors and pre-`backend`
        # conformance fixtures round-trip byte-for-byte.
        if not self.backend.is_default():
            wire["backend"] = self.backend.value
        return wire

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ShmBlobRef:
        raw_backend = d.get("backend")
        backend = (
            BlobBackendKind.from_wire(raw_backend)
            if raw_backend is not None
            else BlobBackendKind.SHM
        )
        return cls(
            offset=d["offset"],
            len=d["len"],
            generation=d["generation"],
            epoch=d["epoch"],
            checksum=d["checksum"],
            backend=backend,
        )


# ---------------------------------------------------------------------------
# Shared-memory blob arena (parity with lazily-rs / lazily-zig)
# ---------------------------------------------------------------------------

#: Bytes reserved before every shared-memory blob payload. Matches the 40-byte
#: header written by ``lazily-rs`` ``ShmBlobArena`` and ``lazily-zig``
#: ``ShmBlobArena`` so descriptors interoperate across siblings.
SHM_BLOB_HEADER_LEN = 40

_SHM_BLOB_MAGIC = 0x4C5A5348  # "LZSH"
_SHM_BLOB_VERSION = 1
_FNV_OFFSET_BASIS = 0xCBF29CE484222325
_FNV_PRIME = 0x00000100000001B3
_U64_MASK = (1 << 64) - 1
_SHM_BLOB_MIN_CAPACITY = SHM_BLOB_HEADER_LEN + 1


class ShmBlobArenaError(Exception):
    """Base class for errors raised by :class:`ShmBlobArena`.

    Each failure mode has a concrete subclass; catch
    :class:`ShmBlobArenaError` to handle any arena failure. The variants mirror
    the ``lazily-rs`` ``ShmBlobArenaError`` enum and the ``lazily-zig``
    ``ShmBlobArenaError`` error set.
    """


class ShmBlobCapacityTooSmall(ShmBlobArenaError):
    """The backing buffer cannot hold one header plus one payload byte."""

    def __init__(self, capacity: int, min_capacity: int) -> None:
        self.capacity = capacity
        self.min_capacity = min_capacity
        super().__init__(
            f"SHM blob arena capacity {capacity} is smaller than minimum {min_capacity}"
        )


class ShmBlobTooLarge(ShmBlobArenaError):
    """Payload is larger than the largest single blob this arena can hold."""

    def __init__(self, length: int, max_length: int) -> None:
        self.length = length
        self.max_length = max_length
        super().__init__(f"SHM blob length {length} exceeds maximum {max_length}")


class ShmBlobDescriptorOutOfBounds(ShmBlobArenaError):
    """Descriptor points outside this arena."""

    def __init__(self, offset: int, length: int, capacity: int) -> None:
        self.offset = offset
        self.length = length
        self.capacity = capacity
        super().__init__(
            f"SHM blob descriptor offset={offset} len={length} exceeds arena "
            f"capacity {capacity}"
        )


class ShmBlobDescriptorMismatch(ShmBlobArenaError):
    """Descriptor/header metadata did not match (e.g. stale after wraparound)."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"SHM blob descriptor mismatch for {field}")


class ShmBlobChecksumMismatch(ShmBlobArenaError):
    """Payload checksum did not match the descriptor/header checksum."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"SHM blob checksum mismatch: expected {expected:#x}, got {actual:#x}"
        )


class ShmBlobGenerationOverflow(ShmBlobArenaError):
    """The arena generation counter overflowed ``u64``."""

    def __init__(self) -> None:
        super().__init__("SHM blob generation counter overflowed")


def _fnv1a_64(payload: bytes | bytearray | memoryview) -> int:
    """FNV-1a (64-bit) non-cryptographic checksum, matching lazily-rs/zig."""
    hash_value = _FNV_OFFSET_BASIS
    for byte in payload:
        hash_value = ((hash_value ^ byte) * _FNV_PRIME) & _U64_MASK
    return hash_value


def _write_blob_header(buffer: bytearray, offset: int, descriptor: ShmBlobRef) -> None:
    struct.pack_into(
        "<IHHQQQQ",
        buffer,
        offset,
        _SHM_BLOB_MAGIC,
        _SHM_BLOB_VERSION,
        SHM_BLOB_HEADER_LEN,
        descriptor.generation,
        descriptor.epoch,
        descriptor.len,
        descriptor.checksum,
    )


def _read_blob_header(buffer: bytearray, offset: int) -> ShmBlobRef:
    magic, version, header_len, generation, epoch, length, checksum = (
        struct.unpack_from("<IHHQQQQ", buffer, offset)
    )
    if magic != _SHM_BLOB_MAGIC:
        raise ShmBlobDescriptorMismatch("magic")
    if version != _SHM_BLOB_VERSION:
        raise ShmBlobDescriptorMismatch("version")
    if header_len != SHM_BLOB_HEADER_LEN:
        raise ShmBlobDescriptorMismatch("header_len")
    return ShmBlobRef(
        offset=offset,
        generation=generation,
        epoch=epoch,
        len=length,
        checksum=checksum,
    )


def _blob_mismatch_field(actual: ShmBlobRef, expected: ShmBlobRef) -> str:
    if actual.generation != expected.generation:
        return "generation"
    if actual.epoch != expected.epoch:
        return "epoch"
    if actual.len != expected.len:
        return "len"
    if actual.checksum != expected.checksum:
        return "checksum"
    return "offset"


class ShmBlobArena:
    """Fixed-size blob arena suitable for a shared-memory transport.

    Ports ``lazily-rs`` ``ShmBlobArena<B>`` (``ipc.rs``) and mirrors
    ``lazily-zig`` ``ShmBlobArena`` (``ipc.zig``): a flat byte buffer plus an
    append-only write cursor and fixed-size :class:`ShmBlobRef` descriptors.

    The arena writes a 40-byte header before each payload. Readers validate the
    header, generation, epoch, payload length, and FNV-1a checksum before
    returning a view. Writes are append-only with wraparound; each write bumps a
    generation counter so a stale descriptor that lands on an overwritten region
    fails validation instead of returning torn data.

    Backing is a :class:`bytearray` (no native extension — preserves the
    pure-Python install story). :meth:`from_buffer` wraps externally-owned
    storage; the caller remains responsible for that buffer's lifetime. True
    cross-process OS shared memory (``/dev/shm``, ``mmap``) is a follow-on that
    swaps the backing buffer.
    """

    __slots__ = ("_buffer", "_next_generation", "_write_offset")

    def __init__(self, buffer: bytearray) -> None:
        capacity = len(buffer)
        if capacity < _SHM_BLOB_MIN_CAPACITY:
            raise ShmBlobCapacityTooSmall(capacity, _SHM_BLOB_MIN_CAPACITY)
        self._buffer = buffer
        self._write_offset = 0
        self._next_generation = 1

    @classmethod
    def with_capacity(cls, capacity: int) -> ShmBlobArena:
        """Create a ``bytearray``-backed arena of ``capacity`` bytes."""
        if capacity < _SHM_BLOB_MIN_CAPACITY:
            raise ShmBlobCapacityTooSmall(capacity, _SHM_BLOB_MIN_CAPACITY)
        return cls(bytearray(capacity))

    @classmethod
    def from_buffer(cls, buffer: bytearray) -> ShmBlobArena:
        """Wrap an existing ``bytearray`` (externally-owned, not zeroed).

        The caller keeps ownership of ``buffer``; the arena reads and writes it
        in place. Use this to back an arena with OS shared memory once a
        transport swaps the buffer in.
        """
        return cls(buffer)

    @property
    def capacity(self) -> int:
        """Total arena capacity in bytes."""
        return len(self._buffer)

    @property
    def max_blob_len(self) -> int:
        """Maximum payload length this arena can hold in one blob."""
        return self.capacity - SHM_BLOB_HEADER_LEN

    @property
    def write_offset(self) -> int:
        """Current write cursor offset."""
        return self._write_offset

    def buffer(self) -> memoryview:
        """Read-only view of the backing bytes (for transport setup/inspection)."""
        return memoryview(self._buffer).toreadonly()

    def write_blob(
        self, epoch: int, payload: bytes | bytearray | memoryview
    ) -> ShmBlobRef:
        """Write a payload and return a descriptor suitable for an IPC message."""
        capacity = self.capacity
        length = len(payload)
        max_len = self.max_blob_len
        if length > max_len:
            raise ShmBlobTooLarge(length, max_len)

        total_len = SHM_BLOB_HEADER_LEN + length
        if self._write_offset + total_len > capacity:
            self._write_offset = 0

        generation = self._next_generation
        if generation == _U64_MASK:
            raise ShmBlobGenerationOverflow()
        self._next_generation = generation + 1

        offset = self._write_offset
        checksum = _fnv1a_64(payload)
        descriptor = ShmBlobRef(
            offset=offset,
            len=length,
            generation=generation,
            epoch=epoch,
            checksum=checksum,
        )

        payload_offset = offset + SHM_BLOB_HEADER_LEN
        _write_blob_header(self._buffer, offset, descriptor)
        self._buffer[payload_offset : payload_offset + length] = payload

        self._write_offset += total_len
        if self._write_offset == capacity:
            self._write_offset = 0

        return descriptor

    def read_blob(self, descriptor: ShmBlobRef) -> memoryview:
        """Read and validate a previously written blob; returns a zero-copy view."""
        capacity = self.capacity
        offset = descriptor.offset
        length = descriptor.len
        if offset < 0 or length < 0:
            raise ShmBlobDescriptorOutOfBounds(offset, length, capacity)
        total_len = SHM_BLOB_HEADER_LEN + length
        if offset > capacity or total_len > capacity or offset > capacity - total_len:
            raise ShmBlobDescriptorOutOfBounds(offset, length, capacity)

        header = _read_blob_header(self._buffer, offset)
        # The arena header does not store `backend`; align it to the descriptor
        # so a non-Shm descriptor validates against the backend-agnostic header.
        header = replace(header, backend=descriptor.backend)
        if header != descriptor:
            raise ShmBlobDescriptorMismatch(_blob_mismatch_field(header, descriptor))

        payload_offset = offset + SHM_BLOB_HEADER_LEN
        payload = memoryview(self._buffer)[payload_offset : payload_offset + length]
        actual = _fnv1a_64(payload)
        if actual != descriptor.checksum:
            raise ShmBlobChecksumMismatch(descriptor.checksum, actual)
        return payload.toreadonly()


# ---------------------------------------------------------------------------
# NodeState (externally-tagged enum: Payload | SharedBlob | Opaque)
# ---------------------------------------------------------------------------


class NodeState:
    """Serializable state for one allowlisted node in a Snapshot or ``NodeAdd``.

    A tagged union with three variants:

    - :class:`NodeState_Payload` — concrete serialized value bytes
    - :class:`NodeState_SharedBlob` — descriptor into a shared-memory arena
    - :class:`NodeState_Opaque` — a known node whose value cannot be serialized
    """

    def to_wire(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def from_wire(value: Any) -> NodeState:
        # Unit variant `Opaque` serializes as a bare string.
        if isinstance(value, str):
            if value == "Opaque":
                return NodeState_Opaque()
            raise ValueError(f"unknown NodeState unit variant: {value!r}")
        if isinstance(value, dict) and len(value) == 1:
            tag, body = next(iter(value.items()))
            if tag == "Payload":
                return NodeState_Payload(_bytes_from_wire(body))
            if tag == "SharedBlob":
                return NodeState_SharedBlob(ShmBlobRef.from_wire(body))
            if tag == "Opaque":
                return NodeState_Opaque()
            raise ValueError(f"unknown NodeState variant: {tag!r}")
        raise ValueError(f"malformed NodeState wire value: {value!r}")


@dataclass(frozen=True)
class NodeState_Payload(NodeState):
    """Concrete serialized value bytes."""

    data: bytes

    def to_wire(self) -> dict[str, list[int]]:
        return {"Payload": _bytes_to_wire(self.data)}


@dataclass(frozen=True)
class NodeState_SharedBlob(NodeState):
    """Descriptor for a value stored in a shared-memory blob arena."""

    blob: ShmBlobRef

    def to_wire(self) -> dict[str, dict[str, int | str]]:
        return {"SharedBlob": self.blob.to_wire()}


@dataclass(frozen=True)
class NodeState_Opaque(NodeState):
    """A known node whose value cannot be serialized."""

    def to_wire(self) -> str:
        return "Opaque"


# ---------------------------------------------------------------------------
# IpcValue (externally-tagged enum: Inline | SharedBlob)
# ---------------------------------------------------------------------------


class IpcValue:
    """A delta value carried inline or by shared-memory blob reference."""

    def to_wire(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def from_wire(value: Any) -> IpcValue:
        if isinstance(value, dict) and len(value) == 1:
            tag, body = next(iter(value.items()))
            if tag == "Inline":
                return IpcValue_Inline(_bytes_from_wire(body))
            if tag == "SharedBlob":
                return IpcValue_SharedBlob(ShmBlobRef.from_wire(body))
            raise ValueError(f"unknown IpcValue variant: {tag!r}")
        raise ValueError(f"malformed IpcValue wire value: {value!r}")

    @staticmethod
    def of(value: IpcValue | ShmBlobRef | bytes | bytearray) -> IpcValue:
        """Coerce bytes / a blob ref into an :class:`IpcValue`."""
        if isinstance(value, IpcValue):
            return value
        if isinstance(value, ShmBlobRef):
            return IpcValue_SharedBlob(value)
        if isinstance(value, (bytes, bytearray)):
            return IpcValue_Inline(bytes(value))
        raise TypeError(f"cannot coerce {type(value).__name__} into IpcValue")


@dataclass(frozen=True)
class IpcValue_Inline(IpcValue):
    """Inline serialized bytes."""

    data: bytes

    def to_wire(self) -> dict[str, list[int]]:
        return {"Inline": _bytes_to_wire(self.data)}


@dataclass(frozen=True)
class IpcValue_SharedBlob(IpcValue):
    """Descriptor for bytes stored in a shared-memory blob arena."""

    blob: ShmBlobRef

    def to_wire(self) -> dict[str, dict[str, int | str]]:
        return {"SharedBlob": self.blob.to_wire()}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """Full state for one node in a snapshot."""

    node: NodeId
    type_tag: str
    state: NodeState
    key: NodeKey | None = None

    @classmethod
    def payload(cls, node: NodeId, type_tag: str, data: bytes) -> NodeSnapshot:
        """A visible node carrying serialized value bytes."""
        return cls(node, type_tag, NodeState_Payload(bytes(data)))

    @classmethod
    def opaque(cls, node: NodeId, type_tag: str) -> NodeSnapshot:
        """A visible node whose value cannot be serialized."""
        return cls(node, type_tag, NodeState_Opaque())

    @classmethod
    def shared_blob(cls, node: NodeId, type_tag: str, blob: ShmBlobRef) -> NodeSnapshot:
        """A visible node whose value lives in a shared-memory blob arena."""
        return cls(node, type_tag, NodeState_SharedBlob(blob))

    def with_key(self, key: NodeKey) -> NodeSnapshot:
        """Return a copy carrying a wire-stable :class:`NodeKey` (builder style)."""
        return NodeSnapshot(self.node, self.type_tag, self.state, key)

    def to_wire(self) -> dict[str, Any]:
        # Self-describing codecs (JSON, MessagePack) omit a `None` key so
        # pre-`key` encoders and existing conformance fixtures round-trip
        # unchanged.
        wire: dict[str, Any] = {
            "node": self.node,
            "type_tag": self.type_tag,
            "state": self.state.to_wire(),
        }
        if self.key is not None:
            wire["key"] = self.key.to_wire()
        return wire

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> NodeSnapshot:
        return cls(
            node=d["node"],
            type_tag=d["type_tag"],
            state=NodeState.from_wire(d["state"]),
            key=NodeKey.from_wire(d["key"]) if "key" in d else None,
        )


@dataclass(frozen=True, slots=True)
class EdgeSnapshot:
    """Directed dependency edge (``dependent`` → ``dependency``)."""

    dependent: NodeId
    dependency: NodeId

    def to_wire(self) -> dict[str, NodeId]:
        return {"dependent": self.dependent, "dependency": self.dependency}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> EdgeSnapshot:
        return cls(dependent=d["dependent"], dependency=d["dependency"])

    def _is_readable_by(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.dependent) and permissions.can_read(
            peer, self.dependency
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Full graph image sent on connect or resync."""

    epoch: int
    nodes: list[NodeSnapshot] = field(default_factory=list)
    edges: list[EdgeSnapshot] = field(default_factory=list)
    roots: list[NodeId] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "nodes": [n.to_wire() for n in self.nodes],
            "edges": [e.to_wire() for e in self.edges],
            "roots": list(self.roots),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> Snapshot:
        return cls(
            epoch=d["epoch"],
            nodes=[NodeSnapshot.from_wire(n) for n in d.get("nodes", [])],
            edges=[EdgeSnapshot.from_wire(e) for e in d.get("edges", [])],
            roots=list(d.get("roots", [])),
        )

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> Snapshot:
        """Peer-specific snapshot that **omits** non-readable nodes entirely.

        Edges are retained only when both endpoints are readable; roots preserve
        their input order after filtering. Non-readable nodes are dropped, not
        redacted in place, so a peer cannot infer their existence.
        """
        nodes = [n for n in self.nodes if permissions.can_read(peer, n.node)]
        edges = [e for e in self.edges if e._is_readable_by(permissions, peer)]
        roots = permissions.filter_readable(peer, self.roots)
        return Snapshot(epoch=self.epoch, nodes=nodes, edges=edges, roots=roots)


# ---------------------------------------------------------------------------
# Delta ops (externally-tagged enum, 7 variants)
# ---------------------------------------------------------------------------


class DeltaOp:
    """One incremental graph mutation in a :class:`Delta`."""

    def to_wire(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        raise NotImplementedError

    # --- constructors mirroring the Rust helper surface ---

    @staticmethod
    def cell_set(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        return DeltaOp_CellSet(node, IpcValue.of(payload))

    @staticmethod
    def slot_value(node: NodeId, payload: IpcValue | ShmBlobRef | bytes) -> DeltaOp:
        return DeltaOp_SlotValue(node, IpcValue.of(payload))

    @staticmethod
    def invalidate(node: NodeId) -> DeltaOp:
        return DeltaOp_Invalidate(node)

    @staticmethod
    def node_add(
        node: NodeId,
        type_tag: str,
        state: NodeState,
        key: NodeKey | None = None,
    ) -> DeltaOp:
        return DeltaOp_NodeAdd(node, type_tag, state, key)

    @staticmethod
    def node_remove(node: NodeId) -> DeltaOp:
        return DeltaOp_NodeRemove(node)

    @staticmethod
    def edge_add(dependent: NodeId, dependency: NodeId) -> DeltaOp:
        return DeltaOp_EdgeAdd(dependent, dependency)

    @staticmethod
    def edge_remove(dependent: NodeId, dependency: NodeId) -> DeltaOp:
        return DeltaOp_EdgeRemove(dependent, dependency)

    @staticmethod
    def from_wire(value: Any) -> DeltaOp:
        if not (isinstance(value, dict) and len(value) == 1):
            raise ValueError(f"malformed DeltaOp wire value: {value!r}")
        tag, body = next(iter(value.items()))
        if tag == "CellSet":
            return DeltaOp_CellSet(body["node"], IpcValue.from_wire(body["payload"]))
        if tag == "SlotValue":
            return DeltaOp_SlotValue(body["node"], IpcValue.from_wire(body["payload"]))
        if tag == "Invalidate":
            return DeltaOp_Invalidate(body["node"])
        if tag == "NodeAdd":
            return DeltaOp_NodeAdd(
                body["node"],
                body["type_tag"],
                NodeState.from_wire(body["state"]),
                NodeKey.from_wire(body["key"]) if "key" in body else None,
            )
        if tag == "NodeRemove":
            return DeltaOp_NodeRemove(body["node"])
        if tag == "EdgeAdd":
            return DeltaOp_EdgeAdd(body["dependent"], body["dependency"])
        if tag == "EdgeRemove":
            return DeltaOp_EdgeRemove(body["dependent"], body["dependency"])
        raise ValueError(f"unknown DeltaOp variant: {tag!r}")


@dataclass(frozen=True)
class DeltaOp_CellSet(DeltaOp):
    """A source cell was changed to ``payload`` (PartialEq-guarded)."""

    node: NodeId
    payload: IpcValue

    def to_wire(self) -> dict[str, Any]:
        return {"CellSet": {"node": self.node, "payload": self.payload.to_wire()}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_SlotValue(DeltaOp):
    """A lazily recomputed slot published a concrete value."""

    node: NodeId
    payload: IpcValue

    def to_wire(self) -> dict[str, Any]:
        return {"SlotValue": {"node": self.node, "payload": self.payload.to_wire()}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_Invalidate(DeltaOp):
    """A node was dirtied without publishing a concrete value (lazy)."""

    node: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {"Invalidate": {"node": self.node}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_NodeAdd(DeltaOp):
    """A new node became visible (use ``NodeState_Opaque`` for an unset node)."""

    node: NodeId
    type_tag: str
    state: NodeState
    key: NodeKey | None = None

    def to_wire(self) -> dict[str, Any]:
        # Self-describing codecs omit a `None` key (matches NodeSnapshot).
        body: dict[str, Any] = {
            "node": self.node,
            "type_tag": self.type_tag,
            "state": self.state.to_wire(),
        }
        if self.key is not None:
            body["key"] = self.key.to_wire()
        return {"NodeAdd": body}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_NodeRemove(DeltaOp):
    """A node was removed (free-list reuse: Remove then Add)."""

    node: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {"NodeRemove": {"node": self.node}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.node)


@dataclass(frozen=True)
class DeltaOp_EdgeAdd(DeltaOp):
    """A dependency edge was added."""

    dependent: NodeId
    dependency: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {"EdgeAdd": {"dependent": self.dependent, "dependency": self.dependency}}

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.dependent) and permissions.can_read(
            peer, self.dependency
        )


@dataclass(frozen=True)
class DeltaOp_EdgeRemove(DeltaOp):
    """A dependency edge was removed."""

    dependent: NodeId
    dependency: NodeId

    def to_wire(self) -> dict[str, Any]:
        return {
            "EdgeRemove": {"dependent": self.dependent, "dependency": self.dependency}
        }

    def _target_readable(self, permissions: PeerPermissions, peer: PeerId) -> bool:
        return permissions.can_read(peer, self.dependent) and permissions.can_read(
            peer, self.dependency
        )


# ---------------------------------------------------------------------------
# Delta + receiver decision
# ---------------------------------------------------------------------------


class DeltaApplyStatusKind(Enum):
    APPLY = "apply"
    RESYNC_REQUIRED = "resync_required"


@dataclass(frozen=True, slots=True)
class DeltaApplyStatus:
    """Receiver decision for an incoming :class:`Delta`."""

    kind: DeltaApplyStatusKind
    last_epoch: int | None = None
    base_epoch: int | None = None
    epoch: int | None = None

    @classmethod
    def apply(cls) -> DeltaApplyStatus:
        return cls(DeltaApplyStatusKind.APPLY)

    @classmethod
    def resync_required(
        cls, last_epoch: int, base_epoch: int, epoch: int
    ) -> DeltaApplyStatus:
        return cls(
            DeltaApplyStatusKind.RESYNC_REQUIRED,
            last_epoch=last_epoch,
            base_epoch=base_epoch,
            epoch=epoch,
        )

    @property
    def is_apply(self) -> bool:
        return self.kind is DeltaApplyStatusKind.APPLY

    @property
    def is_resync_required(self) -> bool:
        return self.kind is DeltaApplyStatusKind.RESYNC_REQUIRED


@dataclass(frozen=True, slots=True)
class Delta:
    """Incremental change set emitted after one outermost batch flush."""

    base_epoch: int
    epoch: int
    ops: list[DeltaOp] = field(default_factory=list)

    @classmethod
    def next(cls, base_epoch: int, ops: list[DeltaOp]) -> Delta:
        """Strictly sequential delta with ``epoch == base_epoch + 1``."""
        return cls(base_epoch=base_epoch, epoch=base_epoch + 1, ops=list(ops))

    @classmethod
    def new(cls, base_epoch: int, epoch: int, ops: list[DeltaOp]) -> Delta:
        return cls(base_epoch=base_epoch, epoch=epoch, ops=list(ops))

    def is_next_after(self, last_epoch: int) -> bool:
        """Whether this delta is exactly the next delta after ``last_epoch``."""
        return self.base_epoch == last_epoch and self.epoch == self.base_epoch + 1

    def apply_status(self, last_epoch: int) -> DeltaApplyStatus:
        """The receiver action for this delta given its current ``last_epoch``."""
        if self.is_next_after(last_epoch):
            return DeltaApplyStatus.apply()
        return DeltaApplyStatus.resync_required(
            last_epoch=last_epoch, base_epoch=self.base_epoch, epoch=self.epoch
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "epoch": self.epoch,
            "ops": [op.to_wire() for op in self.ops],
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> Delta:
        return cls(
            base_epoch=d["base_epoch"],
            epoch=d["epoch"],
            ops=[DeltaOp.from_wire(op) for op in d.get("ops", [])],
        )

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> Delta:
        """Peer-specific delta that omits non-readable operations entirely."""
        ops = [op for op in self.ops if op._target_readable(permissions, peer)]
        return Delta(base_epoch=self.base_epoch, epoch=self.epoch, ops=ops)


# ---------------------------------------------------------------------------
# Distributed: CRDT cell plane (CrdtSync)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WireStamp:
    """Wire mirror of the runtime HLC stamp — a total order ``(wall, logical, peer)``.

    All plain integers so the wire format is codec-stable whether or not a peer
    compiles the CRDT runtime in. Round-trips across all codecs (JSON,
    MessagePack, Postcard).
    """

    wall_time: int
    logical: int
    peer: int

    def to_wire(self) -> dict[str, int]:
        return {
            "wall_time": self.wall_time,
            "logical": self.logical,
            "peer": self.peer,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> WireStamp:
        return cls(
            wall_time=d["wall_time"],
            logical=d["logical"],
            peer=d["peer"],
        )


@dataclass(frozen=True, slots=True)
class CrdtOp:
    """One CRDT cell op on the wire (state-based / CvRDT).

    The converged register, sequence, or text ``state`` for ``node``, tagged
    with the :class:`WireStamp` that produced it and an optional wire-stable
    :class:`NodeKey` that survives NodeId churn. The receiver merges ``state``
    into its local replica; because every cell CRDT merge is commutative,
    associative, and idempotent, out-of-order, duplicated, or batched delivery
    all converge — so a :class:`CrdtOp` is safe to resend.
    """

    node: NodeId
    key: NodeKey | None
    stamp: WireStamp
    state: IpcValue

    @classmethod
    def new(
        cls, node: NodeId, stamp: WireStamp, state: IpcValue | ShmBlobRef | bytes
    ) -> CrdtOp:
        """Construct a keyless op (addressed only by ``node``)."""
        return cls(node, None, stamp, IpcValue.of(state))

    @classmethod
    def keyed(
        cls,
        node: NodeId,
        key: NodeKey,
        stamp: WireStamp,
        state: IpcValue | ShmBlobRef | bytes,
    ) -> CrdtOp:
        """Construct an op carrying a wire-stable :class:`NodeKey`."""
        return cls(node, key, stamp, IpcValue.of(state))

    def to_wire(self) -> dict[str, Any]:
        # Mirrors the lazily-rs derived serde struct: `key` is always present
        # (null when unset) so byte output matches the Rust reference. A decoder
        # also accepts an absent field.
        return {
            "node": self.node,
            "key": self.key.to_wire() if self.key is not None else None,
            "stamp": self.stamp.to_wire(),
            "state": self.state.to_wire(),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CrdtOp:
        key = d.get("key")
        return cls(
            node=d["node"],
            key=NodeKey.from_wire(key) if key is not None else None,
            stamp=WireStamp.from_wire(d["stamp"]),
            state=IpcValue.from_wire(d["state"]),
        )


@dataclass(frozen=True, slots=True)
class CrdtSync:
    """A CRDT anti-entropy sync frame (the multi-writer plane).

    The sender advertises its per-peer **stamp frontier** (the highest
    :class:`WireStamp` it has observed from each peer) and ships a batch of
    :class:`CrdtOp` s. The frontier exchange is bounded, idempotent, and
    resumable; re-sending a frame the receiver already has is a no-op.
    """

    frontier: list[tuple[int, WireStamp]] = field(default_factory=list)
    ops: list[CrdtOp] = field(default_factory=list)

    @classmethod
    def new(cls, frontier: list[tuple[int, WireStamp]], ops: list[CrdtOp]) -> CrdtSync:
        """Construct a sync frame from a frontier advertisement and an op batch."""
        return cls(frontier=list(frontier), ops=list(ops))

    def to_wire(self) -> dict[str, Any]:
        return {
            "frontier": [[peer, stamp.to_wire()] for peer, stamp in self.frontier],
            "ops": [op.to_wire() for op in self.ops],
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CrdtSync:
        frontier = [
            (int(entry[0]), WireStamp.from_wire(entry[1]))
            for entry in d.get("frontier", [])
        ]
        ops = [CrdtOp.from_wire(op) for op in d.get("ops", [])]
        return cls(frontier=frontier, ops=ops)

    def filter_readable(self, permissions: PeerPermissions, peer: PeerId) -> CrdtSync:
        """Peer-specific frame that **omits** ops for non-readable nodes entirely.

        Omission, not redaction — mirroring :meth:`Delta.filter_readable`. The
        ``frontier`` advertisement is retained: it names peers and stamps, not
        node content, and the receiver needs the whole frontier to compute a
        sound causal-stability watermark.
        """
        ops = [op for op in self.ops if permissions.can_read(peer, op.node)]
        return CrdtSync(frontier=list(self.frontier), ops=ops)


# ---------------------------------------------------------------------------
# Reliable sync (#lzsync): reverse-channel control frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResyncRequest:
    """Reliable-sync reverse-channel control frame: request a covering
    :class:`Snapshot` on a detected gap (``#lzsync``, spec § ResyncCoordinator).

    Carries no node content, so it is permission-filter- and blob-spill-
    transparent. The requesting receiver's ``from_epoch`` is its ``last_epoch``;
    the sender replies with a ``Snapshot { epoch >= from_epoch }``.
    """

    from_epoch: int

    def to_wire(self) -> dict[str, int]:
        return {"from_epoch": self.from_epoch}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> ResyncRequest:
        return cls(from_epoch=d["from_epoch"])


@dataclass(frozen=True, slots=True)
class OutboxAck:
    """Reliable-sync reverse-channel control frame: prove receipt through
    ``through_epoch`` (``#lzsync``, spec § DurableOutbox).

    Advances the sender's outbox retention cursor and doubles as the reconnect
    resume cursor. Carries no node content.
    """

    through_epoch: int

    def to_wire(self) -> dict[str, int]:
        return {"through_epoch": self.through_epoch}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> OutboxAck:
        return cls(through_epoch=d["through_epoch"])


# ---------------------------------------------------------------------------
# IpcMessage (externally-tagged enum:
#   Snapshot | Delta | CrdtSync | ResyncRequest | OutboxAck)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IpcMessage:
    """Tagged IPC protocol message — a :class:`Snapshot`, :class:`Delta`,
    :class:`CrdtSync`, or a reliable-sync control frame (:class:`ResyncRequest`
    / :class:`OutboxAck`)."""

    snapshot: Snapshot | None = None
    delta: Delta | None = None
    crdt_sync: CrdtSync | None = None
    resync_request: ResyncRequest | None = None
    outbox_ack: OutboxAck | None = None

    @classmethod
    def of_snapshot(cls, snapshot: Snapshot) -> IpcMessage:
        return cls(snapshot=snapshot)

    @classmethod
    def of_delta(cls, delta: Delta) -> IpcMessage:
        return cls(delta=delta)

    @classmethod
    def of_crdt_sync(cls, crdt_sync: CrdtSync) -> IpcMessage:
        return cls(crdt_sync=crdt_sync)

    @classmethod
    def of_resync_request(cls, resync_request: ResyncRequest) -> IpcMessage:
        return cls(resync_request=resync_request)

    @classmethod
    def of_outbox_ack(cls, outbox_ack: OutboxAck) -> IpcMessage:
        return cls(outbox_ack=outbox_ack)

    @property
    def is_snapshot(self) -> bool:
        return self.snapshot is not None

    @property
    def is_delta(self) -> bool:
        return self.delta is not None

    @property
    def is_crdt_sync(self) -> bool:
        return self.crdt_sync is not None

    @property
    def is_resync_request(self) -> bool:
        return self.resync_request is not None

    @property
    def is_outbox_ack(self) -> bool:
        return self.outbox_ack is not None

    @property
    def is_control(self) -> bool:
        """Whether this is a reliable-sync reverse-channel control frame
        (:class:`ResyncRequest` / :class:`OutboxAck`) — no node content, so
        permission filtering and blob spilling are the identity on it."""
        return self.resync_request is not None or self.outbox_ack is not None

    def to_wire(self) -> dict[str, Any]:
        if self.snapshot is not None:
            return {"Snapshot": self.snapshot.to_wire()}
        if self.delta is not None:
            return {"Delta": self.delta.to_wire()}
        if self.crdt_sync is not None:
            return {"CrdtSync": self.crdt_sync.to_wire()}
        if self.resync_request is not None:
            return {"ResyncRequest": self.resync_request.to_wire()}
        if self.outbox_ack is not None:
            return {"OutboxAck": self.outbox_ack.to_wire()}
        raise ValueError(
            "IpcMessage carries no Snapshot, Delta, CrdtSync, "
            "ResyncRequest, nor OutboxAck"
        )

    @classmethod
    def from_wire(cls, value: Any) -> IpcMessage:
        if not (isinstance(value, dict) and len(value) == 1):
            raise ValueError(f"malformed IpcMessage wire value: {value!r}")
        tag, body = next(iter(value.items()))
        if tag == "Snapshot":
            return cls(snapshot=Snapshot.from_wire(body))
        if tag == "Delta":
            return cls(delta=Delta.from_wire(body))
        if tag == "CrdtSync":
            return cls(crdt_sync=CrdtSync.from_wire(body))
        if tag == "ResyncRequest":
            return cls(resync_request=ResyncRequest.from_wire(body))
        if tag == "OutboxAck":
            return cls(outbox_ack=OutboxAck.from_wire(body))
        raise ValueError(f"unknown IpcMessage variant: {tag!r}")

    def encode_json(self) -> bytes:
        """Serialize to transport-agnostic JSON bytes."""
        return json.dumps(self.to_wire(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode_json(cls, data: bytes | str) -> IpcMessage:
        """Parse JSON bytes (or str) produced by any lazily binding."""
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        return cls.from_wire(json.loads(data))

    def encode_msgpack(self) -> bytes:
        """Serialize to ``msgpack`` frame bytes — the cross-language binary
        default (protocol.md § Frame codecs).

        Deliberately serializes the very tree :meth:`to_wire` builds for the
        ``json`` reference codec rather than describing the frame a second
        time. The codec token names one wire — externally tagged envelope over
        named-field maps keyed by the ``json`` field names, with the same
        omit-when-absent rule for the optional ``NodeKey`` — and deriving it
        from ``to_wire()`` makes those properties identical to the reference
        codec by construction. Not byte-canonical: MessagePack map key order is
        encoder-defined, so conformance is ``decode(encode(m)) == m``.
        """
        return msgpack_pack(self.to_wire())

    @classmethod
    def decode_msgpack(cls, data: bytes | bytearray | memoryview) -> IpcMessage:
        """Parse ``msgpack`` frame bytes produced by any lazily binding."""
        return cls.from_wire(msgpack_unpack(data))


# ---------------------------------------------------------------------------
# Permission boundary (RemoteOp allowlist)
# ---------------------------------------------------------------------------


class OpKind(Enum):
    """The category of access a :class:`RemoteOp` requests.

    The three kinds are gated **independently**: a read grant never implies write
    or effect-trigger.
    """

    READ = "read"
    WRITE = "write"
    TRIGGER_EFFECT = "trigger_effect"


@dataclass(frozen=True, slots=True)
class RemoteOp:
    """A single operation a remote peer may request against the shared graph."""

    kind: OpKind
    node: NodeId

    @classmethod
    def read(cls, node: NodeId) -> RemoteOp:
        return cls(OpKind.READ, node)

    @classmethod
    def write(cls, node: NodeId) -> RemoteOp:
        return cls(OpKind.WRITE, node)

    @classmethod
    def trigger_effect(cls, node: NodeId) -> RemoteOp:
        return cls(OpKind.TRIGGER_EFFECT, node)


@dataclass(frozen=True)
class PermissionDenied(Exception):
    """Raised/returned when a peer requests an operation outside its allowlist."""

    peer: PeerId
    op: RemoteOp

    def __str__(self) -> str:
        return f"peer {self.peer} denied {self.op.kind.value} on node {self.op.node}"


class PeerPermissions:
    """Default-deny per-peer allowlist gating reads, writes, and effect triggers.

    Only nodes on a peer's read allowlist are serialized into a snapshot or
    delta; non-allowlisted nodes are omitted entirely.
    """

    __slots__ = ("_peers",)

    def __init__(self) -> None:
        # peer -> {OpKind -> set[NodeId]}
        self._peers: dict[PeerId, dict[OpKind, set[NodeId]]] = {}

    def allow(self, peer: PeerId, op: RemoteOp) -> bool:
        """Grant ``peer`` permission to perform ``op``.

        Returns ``True`` if newly added, ``False`` if already held.
        """
        nodes = self._peers.setdefault(peer, {}).setdefault(op.kind, set())
        if op.node in nodes:
            return False
        nodes.add(op.node)
        return True

    def allow_many(self, peer: PeerId, kind: OpKind, nodes: Iterable[NodeId]) -> None:
        """Grant ``peer`` ``kind`` access over many nodes at once."""
        target = self._peers.setdefault(peer, {}).setdefault(kind, set())
        target.update(nodes)

    def revoke(self, peer: PeerId, op: RemoteOp) -> bool:
        """Revoke ``op`` from ``peer``. Returns ``True`` if it was present."""
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return False
        nodes = peer_perms.get(op.kind)
        if nodes is None or op.node not in nodes:
            return False
        nodes.discard(op.node)
        self._prune(peer)
        return True

    def revoke_peer(self, peer: PeerId) -> bool:
        """Remove every permission held by ``peer`` (e.g. on disconnect)."""
        return self._peers.pop(peer, None) is not None

    def is_allowed(self, peer: PeerId, op: RemoteOp) -> bool:
        """Whether ``peer`` may perform ``op``. Default-deny."""
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return False
        return op.node in peer_perms.get(op.kind, ())

    def check(self, peer: PeerId, op: RemoteOp) -> None:
        """Fail-closed permission check.

        Raises :class:`PermissionDenied` when ``peer`` may not perform ``op``.
        """
        if not self.is_allowed(peer, op):
            raise PermissionDenied(peer, op)

    def can_read(self, peer: PeerId, node: NodeId) -> bool:
        return self.is_allowed(peer, RemoteOp.read(node))

    def filter_readable(self, peer: PeerId, nodes: Iterable[NodeId]) -> list[NodeId]:
        """Retain only the nodes ``peer`` may read, preserving input order."""
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return []
        readable = peer_perms.get(OpKind.READ, set())
        return [node for node in nodes if node in readable]

    def peer_count(self) -> int:
        """Number of peers with at least one permission."""
        return len(self._peers)

    def _prune(self, peer: PeerId) -> None:
        peer_perms = self._peers.get(peer)
        if peer_perms is None:
            return
        for kind in [k for k, v in peer_perms.items() if not v]:
            del peer_perms[kind]
        if not peer_perms:
            del self._peers[peer]


# ---------------------------------------------------------------------------
# Capability negotiation (handshake)
# ---------------------------------------------------------------------------


#: The protocol identifier every ``lazily-ipc`` peer must advertise.
PROTOCOL_ID = "lazily-ipc"
#: The current protocol major version.
PROTOCOL_MAJOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class CapabilityHandshake:
    """Compatibility handshake exchanged before any graph state flows.

    Each non-local session starts with this frame. Serialized as a plain JSON
    object (it is a standalone frame, not an :class:`IpcMessage` variant).
    Peers that disagree on ``protocol_major_version``, ``codec``, or
    ``ordered_reliable`` fail closed before applying any :class:`Snapshot` or
    :class:`Delta`.

    ``fragmentation_supported`` and ``features`` default to off/empty and are
    omitted when absent only if explicitly cleared; the frame otherwise carries
    every field so a peer sees the full advertisement.
    """

    protocol_id: str
    protocol_major_version: int
    codec: str
    max_frame_size: int
    fragmentation_supported: bool = False
    ordered_reliable: bool = True
    peer_id: PeerId = 0
    session_id: str = ""
    features: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, peer_id: PeerId, session_id: str) -> CapabilityHandshake:
        """Create a handshake with protocol defaults (JSON codec, 1 MiB frame,
        ordered-reliable, no features)."""
        return cls(
            protocol_id=PROTOCOL_ID,
            protocol_major_version=PROTOCOL_MAJOR_VERSION,
            codec="json",
            max_frame_size=1_048_576,
            fragmentation_supported=False,
            ordered_reliable=True,
            peer_id=peer_id,
            session_id=session_id,
            features=[],
        )

    def with_codec(self, codec: str) -> CapabilityHandshake:
        """Return a copy with the codec negotiation token set."""
        return replace(self, codec=codec)

    def with_max_frame_size(self, max_frame_size: int) -> CapabilityHandshake:
        """Return a copy with the max frame size set."""
        return replace(self, max_frame_size=max_frame_size)

    def with_features(self, features: Iterable[str]) -> CapabilityHandshake:
        """Return a copy with the features list set."""
        return replace(self, features=list(features))

    def with_fragmentation(self, supported: bool) -> CapabilityHandshake:
        """Return a copy with fragmentation support set."""
        return replace(self, fragmentation_supported=supported)

    def has_feature(self, feature: str) -> bool:
        """Whether this peer advertises ``feature``."""
        return feature in self.features

    def is_compatible_with(self, other: CapabilityHandshake) -> bool:
        """Whether this handshake is mutually compatible with ``other``.

        Peers are compatible when both advertise :data:`PROTOCOL_ID`, both
        advertise :data:`PROTOCOL_MAJOR_VERSION`, their major versions and
        codecs agree, and both require ordered reliable delivery. Feature
        negotiation is caller-driven via :attr:`features` /
        :meth:`has_feature`.
        """
        return (
            self.protocol_id == PROTOCOL_ID
            and other.protocol_id == PROTOCOL_ID
            and self.protocol_major_version == PROTOCOL_MAJOR_VERSION
            and other.protocol_major_version == PROTOCOL_MAJOR_VERSION
            and self.protocol_major_version == other.protocol_major_version
            and self.codec == other.codec
            and self.ordered_reliable
            and other.ordered_reliable
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_major_version": self.protocol_major_version,
            "codec": self.codec,
            "max_frame_size": self.max_frame_size,
            "fragmentation_supported": self.fragmentation_supported,
            "ordered_reliable": self.ordered_reliable,
            "peer_id": self.peer_id,
            "session_id": self.session_id,
            "features": list(self.features),
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CapabilityHandshake:
        return cls(
            protocol_id=d["protocol_id"],
            protocol_major_version=d["protocol_major_version"],
            codec=d["codec"],
            max_frame_size=d["max_frame_size"],
            fragmentation_supported=d.get("fragmentation_supported", False),
            ordered_reliable=d.get("ordered_reliable", True),
            peer_id=d["peer_id"],
            session_id=d["session_id"],
            features=list(d.get("features", [])),
        )


# ---------------------------------------------------------------------------
# Causal receipts (generic outcome projection — NOT a transport ACK)
# ---------------------------------------------------------------------------


class ReceiptOutcome(Enum):
    """Generic receipt outcome vocabulary.

    Mirrors ``LazilyFormal.Receipt.ReceiptOutcome`` and
    ``lazily-spec/protocol.md § Causal Receipts``. ``observed`` and
    ``accepted`` are **non-terminal** (an ACK-like transport/queue observation,
    never proof an effect happened); ``applied`` and ``rejected`` are
    **terminal** (the generic outcome a domain fact refines).
    """

    OBSERVED = "observed"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        """Whether this outcome completes the causation projection."""
        return self in (ReceiptOutcome.APPLIED, ReceiptOutcome.REJECTED)

    @classmethod
    def from_wire(cls, value: str) -> ReceiptOutcome:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"unknown receipt outcome: {value!r} "
                "(expected one of observed/accepted/applied/rejected)"
            ) from exc


@dataclass(frozen=True, slots=True)
class CausalReceipt:
    """One causal receipt event — an idempotent observation of a command/effect.

    A receipt records that an ``observer`` (peer, process, or subsystem) saw a
    particular ``causation_id`` at a producer/editor ``generation`` and resolved
    it to an :class:`ReceiptOutcome`. The primitive is projection data: it is
    deliberately **not** a transport ACK, and a non-terminal
    ``observed``/``accepted`` receipt is never authority that an effect
    happened. Terminal ``applied``/``rejected`` receipts are the generic outcome
    vocabulary that domain-specific facts refine.

    ``receipt_id`` is the idempotency key — duplicates are no-ops.
    ``payload_hash`` is an optional hash of the state/payload the receipt
    observed; ``reason`` is an optional human/debug rejection reason. Both are
    ``None`` when absent and serialize as JSON ``null``.
    """

    receipt_id: str
    causation_id: str
    observer: str
    generation: int
    outcome: ReceiptOutcome
    reason: str | None = None
    payload_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.receipt_id:
            raise ValueError("receipt_id must be a non-empty string")
        if not self.causation_id:
            raise ValueError("causation_id must be a non-empty string")
        if not self.observer:
            raise ValueError("observer must be a non-empty string")
        if self.generation < 0:
            raise ValueError(f"generation must be >= 0, got {self.generation}")

    def to_wire(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "causation_id": self.causation_id,
            "observer": self.observer,
            "generation": self.generation,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CausalReceipt:
        return cls(
            receipt_id=d["receipt_id"],
            causation_id=d["causation_id"],
            observer=d["observer"],
            generation=d["generation"],
            outcome=ReceiptOutcome.from_wire(d["outcome"]),
            reason=d.get("reason"),
            payload_hash=d.get("payload_hash"),
        )


@dataclass(frozen=True, slots=True)
class CausalReceipts:
    """Wire frame carrying a batch of :class:`CausalReceipt` events.

    Serialized as a standalone externally-tagged JSON object
    (``{"CausalReceipts": {"receipts": [...]}}``) — like
    :class:`CapabilityHandshake`, a :class:`CausalReceipts` frame is **not** an
    :class:`IpcMessage` variant; transports may carry it on any channel without
    touching the Snapshot/Delta/CrdtSync envelope. A frame may carry receipts
    for several ``causation_id`` s; :meth:`group_by_causation` splits them for
    per-causation projection.
    """

    receipts: list[CausalReceipt] = field(default_factory=list)

    def to_wire(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        return {"CausalReceipts": {"receipts": [r.to_wire() for r in self.receipts]}}

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> CausalReceipts:
        if not (isinstance(d, dict) and set(d.keys()) == {"CausalReceipts"}):
            raise ValueError(f"malformed CausalReceipts wire value: {d!r}")
        body = d["CausalReceipts"]
        return cls(
            receipts=[CausalReceipt.from_wire(r) for r in body.get("receipts", [])]
        )

    def group_by_causation(self) -> dict[str, list[CausalReceipt]]:
        """Group the frame's receipts by ``causation_id``.

        The map a caller iterates to build one :class:`ReceiptProjection` per
        causation id. Insertion order is the order receipts appear in the frame.
        """
        groups: dict[str, list[CausalReceipt]] = {}
        for receipt in self.receipts:
            groups.setdefault(receipt.causation_id, []).append(receipt)
        return groups

    def encode_json(self) -> bytes:
        """Serialize to transport-agnostic JSON bytes."""
        return json.dumps(self.to_wire(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode_json(cls, data: bytes | str) -> CausalReceipts:
        """Parse JSON bytes (or str) produced by any lazily binding."""
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        return cls.from_wire(json.loads(data))


class ReceiptApplyResult(Enum):
    """Result of applying one receipt to a :class:`ReceiptProjection`.

    Mirrors ``LazilyFormal.Receipt.ApplyResult``. Only ``RECORDED`` mutates the
    authoritative terminal projection; the other variants are no-ops on the
    terminal state (the receipt may still be retained as audit/debug data).
    """

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    STALE_GENERATION = "stale_generation"
    TERMINAL_CONFLICT = "terminal_conflict"


class ReceiptProjection:
    """Authoritative outcome projection for one ``causation_id``.

    A pure reducer that folds :class:`CausalReceipt` events into the current
    outcome for a causation id, mirroring ``LazilyFormal.Receipt.apply``. The
    rules from ``lazily-spec/protocol.md § Causal Receipts``:

    * ``observed`` and ``accepted`` are **non-terminal**. They never complete
      the causation and never conflict with a terminal outcome.
    * ``applied`` and ``rejected`` are **terminal**. The first terminal receipt
      for a generation fixes the outcome; a second terminal receipt with a
      *different* outcome is a **terminal conflict** (fail closed, no winner).
    * A receipt whose ``generation`` differs from the authority's current
      generation is **stale** and ignored by the current projection.
    * A duplicate ``receipt_id`` is an idempotent no-op.

    The authority ``current_generation`` is supplied by the caller (the consumer
    that knows the producer/editor generation for the causation id). When
    projecting a frame without external authority, :meth:`from_receipts`
    defaults ``current_generation`` to the maximum generation seen among the
    receipts for the causation id — the natural choice that makes older
    generations stale.
    """

    __slots__ = (
        "_conflicts",
        "_recorded",
        "_seen",
        "_stale",
        "_terminal",
        "causation_id",
        "current_generation",
    )

    def __init__(self, causation_id: str, current_generation: int) -> None:
        if not causation_id:
            raise ValueError("causation_id must be a non-empty string")
        if current_generation < 0:
            raise ValueError(
                f"current_generation must be >= 0, got {current_generation}"
            )
        self.causation_id = causation_id
        self.current_generation = current_generation
        self._seen: set[str] = set()
        self._terminal: ReceiptOutcome | None = None
        self._recorded: list[CausalReceipt] = []
        self._stale: list[CausalReceipt] = []
        self._conflicts: list[CausalReceipt] = []

    @classmethod
    def from_receipts(
        cls,
        causation_id: str,
        receipts: Iterable[CausalReceipt],
        current_generation: int | None = None,
    ) -> ReceiptProjection:
        """Build a projection by folding ``receipts`` for one causation id.

        Only receipts whose ``causation_id`` matches are applied (others are
        ignored). When ``current_generation`` is ``None`` it defaults to the
        maximum generation among the matching receipts (or ``0`` when empty),
        the natural authority for a frame-replay without external state.
        """
        matching = [r for r in receipts if r.causation_id == causation_id]
        if current_generation is None:
            current_generation = (
                max((r.generation for r in matching), default=0) if matching else 0
            )
        projection = cls(causation_id, current_generation)
        for receipt in matching:
            projection.apply(receipt)
        return projection

    def apply(self, receipt: CausalReceipt) -> ReceiptApplyResult:
        """Fold one receipt into the projection.

        Returns the :class:`ReceiptApplyResult`. ``RECORDED`` updates the
        authoritative projection (and the recorded/audit trail); every other
        result leaves the terminal outcome untouched but still classifies the
        receipt into the appropriate audit bucket (stale / conflict) so a caller
        can retain it as debug data per the spec.
        """
        if receipt.receipt_id in self._seen:
            return ReceiptApplyResult.DUPLICATE
        self._seen.add(receipt.receipt_id)
        if receipt.generation != self.current_generation:
            self._stale.append(receipt)
            return ReceiptApplyResult.STALE_GENERATION
        if receipt.outcome.is_terminal:
            if self._terminal is None:
                self._terminal = receipt.outcome
                self._recorded.append(receipt)
                return ReceiptApplyResult.RECORDED
            if self._terminal is receipt.outcome:
                self._recorded.append(receipt)
                return ReceiptApplyResult.RECORDED
            self._conflicts.append(receipt)
            return ReceiptApplyResult.TERMINAL_CONFLICT
        self._recorded.append(receipt)
        return ReceiptApplyResult.RECORDED

    @property
    def terminal_outcome(self) -> ReceiptOutcome | None:
        """The current terminal outcome for the causation id, or ``None``."""
        return self._terminal

    @property
    def is_terminal(self) -> bool:
        """Whether a terminal outcome has been recorded for the causation id."""
        return self._terminal is not None

    @property
    def in_conflict(self) -> bool:
        """Whether a conflicting terminal outcome was observed (fail closed)."""
        return bool(self._conflicts)

    def recorded(self) -> list[CausalReceipt]:
        """The receipts the authority projection retained (non-stale)."""
        return list(self._recorded)

    def nonterminal_outcomes(self) -> list[ReceiptOutcome]:
        """Non-terminal outcomes currently recorded, in first-seen order."""
        seen: set[ReceiptOutcome] = set()
        ordered: list[ReceiptOutcome] = []
        for receipt in self._recorded:
            if not receipt.outcome.is_terminal and receipt.outcome not in seen:
                seen.add(receipt.outcome)
                ordered.append(receipt.outcome)
        return ordered

    def stale_receipt_ids(self) -> list[str]:
        """``receipt_id`` s discarded as stale (audit/debug trail only)."""
        return [r.receipt_id for r in self._stale]

    def conflicting_receipt_ids(self) -> list[str]:
        """``receipt_id`` s that hit a terminal conflict (audit trail only)."""
        return [r.receipt_id for r in self._conflicts]
