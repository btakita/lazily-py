"""lazily IPC frame codec — ``msgpack``, the cross-language binary default.

protocol.md § Frame codecs makes ``msgpack`` MUST-level for every binding, and
says plainly that shipping *a* MessagePack codec is not implementing it: the
codec token names ONE wire — the externally tagged frame (``{"Snapshot": …}``)
over named-field maps whose keys are the ``json`` field names, with the same
omit-when-absent rule for optional fields. A codec that packs the same data as
an internally tagged envelope (``{"type": 0, "value": …}``), gives
``NodeState``/``IpcValue`` integer discriminators instead of the
``Payload``/``Inline`` external tags, or uses positional arrays, is a private
codec that happens to use MessagePack framing; a peer that negotiated
``msgpack`` with it would not decode its frames.

This module is deliberately only a *serializer for a value tree*, not a second
description of the frame schema. :meth:`lazily.ipc.IpcMessage.encode_msgpack`
hands it the very tree ``to_wire()`` builds for the ``json`` codec, so the
external tags, the field names and both ``NodeKey`` rules
(``NodeSnapshot``/``NodeAdd`` omit an absent key, ``CrdtOp`` always writes it,
``null`` when unset) are identical to the reference codec *by construction*. A
hand-written second transcription of the same shape is exactly the drift that
produced lazily-cpp's divergent private framing.

Dependency-free on purpose: lazily-py ships with one runtime dependency
(``mypy-extensions``, for the compiled core), and a third-party MessagePack
library would also bring its own struct-mapping opinions to fight — most of
them default to packing ``bytes`` as ``bin`` and mapping objects positionally,
which is the wrong wire here.

Two rules are enforced rather than assumed:

* **Byte payloads are arrays of integers, not MessagePack ``bin``.** That is
  what the reference encoder produces (``rmp_serde`` serializes ``Vec<u8>``
  through serde's default seq impl) and what its decoder accepts, so emitting
  or accepting ``bin`` in a byte-payload position would put lazily-py outside
  the wire it claims to speak. :func:`msgpack_unpack` therefore *rejects*
  ``bin``.
* **Map keys are strings.** A named-field map is the whole point; an integer
  key is a positional encoding wearing a map's clothes.

NOT byte-canonical (§ Frame codecs): a MessagePack map's key order is
encoder-defined, so conformance is ``decode(encode(m)) == m``, never a golden
byte string. This encoder happens to be deterministic — allowed, but not a
property any peer may rely on.
"""

from __future__ import annotations

import struct
from typing import Any


__all__ = ["MsgpackCodecError", "msgpack_pack", "msgpack_unpack"]


class MsgpackCodecError(ValueError):
    """A frame could not be encoded to, or decoded from, the ``msgpack`` wire."""

    def __init__(self, message: str) -> None:
        super().__init__(f"msgpack codec: {message}")


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


def _pack_uint(out: bytearray, value: int) -> None:
    if value < 0x80:
        out.append(value)
    elif value <= 0xFF:
        out += b"\xcc" + struct.pack(">B", value)
    elif value <= 0xFFFF:
        out += b"\xcd" + struct.pack(">H", value)
    elif value <= 0xFFFF_FFFF:
        out += b"\xce" + struct.pack(">I", value)
    elif value <= 0xFFFF_FFFF_FFFF_FFFF:
        out += b"\xcf" + struct.pack(">Q", value)
    else:
        raise MsgpackCodecError(f"integer out of MessagePack range: {value}")


def _pack_int(out: bytearray, value: int) -> None:
    if value >= 0:
        _pack_uint(out, value)
        return
    if value >= -0x20:
        out.append(0xE0 | (value + 0x20))
    elif value >= -0x80:
        out += b"\xd0" + struct.pack(">b", value)
    elif value >= -0x8000:
        out += b"\xd1" + struct.pack(">h", value)
    elif value >= -0x8000_0000:
        out += b"\xd2" + struct.pack(">i", value)
    elif value >= -0x8000_0000_0000_0000:
        out += b"\xd3" + struct.pack(">q", value)
    else:
        raise MsgpackCodecError(f"integer out of MessagePack range: {value}")


def _pack_str(out: bytearray, value: str) -> None:
    raw = value.encode("utf-8")
    size = len(raw)
    if size < 0x20:
        out.append(0xA0 | size)
    elif size <= 0xFF:
        out += b"\xd9" + struct.pack(">B", size)
    elif size <= 0xFFFF:
        out += b"\xda" + struct.pack(">H", size)
    elif size <= 0xFFFF_FFFF:
        out += b"\xdb" + struct.pack(">I", size)
    else:
        raise MsgpackCodecError("string too long for MessagePack")
    out += raw


def _pack_array_header(out: bytearray, size: int) -> None:
    if size < 0x10:
        out.append(0x90 | size)
    elif size <= 0xFFFF:
        out += b"\xdc" + struct.pack(">H", size)
    elif size <= 0xFFFF_FFFF:
        out += b"\xdd" + struct.pack(">I", size)
    else:
        raise MsgpackCodecError("array too long for MessagePack")


def _pack_map_header(out: bytearray, size: int) -> None:
    if size < 0x10:
        out.append(0x80 | size)
    elif size <= 0xFFFF:
        out += b"\xde" + struct.pack(">H", size)
    elif size <= 0xFFFF_FFFF:
        out += b"\xdf" + struct.pack(">I", size)
    else:
        raise MsgpackCodecError("map too long for MessagePack")


def _pack(out: bytearray, value: Any) -> None:
    if value is None:
        out.append(0xC0)
        return
    # `bool` before `int`: it is a subclass, and a frame's booleans must not
    # decay into 0/1 integers on the wire.
    if isinstance(value, bool):
        out.append(0xC3 if value else 0xC2)
        return
    if isinstance(value, int):
        _pack_int(out, value)
        return
    if isinstance(value, str):
        _pack_str(out, value)
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Byte payloads reach here as a `list[int]` from `to_wire()`. A raw
        # `bytes` would pack as `bin`, which no conforming peer reads in this
        # position — refuse rather than emit a frame outside the wire.
        raise MsgpackCodecError(
            "byte payloads are arrays of integers on this wire, not msgpack `bin`"
        )
    if isinstance(value, (list, tuple)):
        _pack_array_header(out, len(value))
        for element in value:
            _pack(out, element)
        return
    if isinstance(value, dict):
        _pack_map_header(out, len(value))
        for key, member in value.items():
            if not isinstance(key, str):
                raise MsgpackCodecError(
                    f"named-field maps require string keys, got {key!r}"
                )
            _pack_str(out, key)
            _pack(out, member)
        return
    if isinstance(value, float):
        # No `IpcMessage` field is floating point (§ IpcMessage: every field is
        # an integer, string, or byte sequence). Refusing keeps a future
        # double-valued field from silently acquiring a wire form nothing
        # agreed on.
        raise MsgpackCodecError("frames carry no floating-point fields")
    raise MsgpackCodecError(f"unsupported value in frame: {type(value).__name__}")


def msgpack_pack(value: Any) -> bytes:
    """Serialize a ``to_wire()`` value tree to ``msgpack`` frame bytes."""
    out = bytearray()
    _pack(out, value)
    return bytes(out)


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        end = self.pos + count
        if end > len(self.data):
            raise MsgpackCodecError("frame truncated")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def uint(self, width: int, fmt: str) -> int:
        return int(struct.unpack(fmt, self.take(width))[0])


def _unpack_str(reader: _Reader, size: int) -> str:
    try:
        return reader.take(size).decode("utf-8")
    except UnicodeDecodeError as error:
        raise MsgpackCodecError("string field is not valid UTF-8") from error


def _unpack_array(reader: _Reader, size: int) -> list[Any]:
    return [_unpack(reader) for _ in range(size)]


def _unpack_map(reader: _Reader, size: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for _ in range(size):
        key = _unpack(reader)
        if not isinstance(key, str):
            raise MsgpackCodecError(
                f"named-field maps require string keys, got {key!r}"
            )
        out[key] = _unpack(reader)
    return out


def _unpack(reader: _Reader) -> Any:  # one arm per MessagePack format family
    tag = reader.byte()
    if tag <= 0x7F:
        return tag
    if tag >= 0xE0:
        return tag - 0x100
    if 0x80 <= tag <= 0x8F:
        return _unpack_map(reader, tag & 0x0F)
    if 0x90 <= tag <= 0x9F:
        return _unpack_array(reader, tag & 0x0F)
    if 0xA0 <= tag <= 0xBF:
        return _unpack_str(reader, tag & 0x1F)
    if tag == 0xC0:
        return None
    if tag == 0xC2:
        return False
    if tag == 0xC3:
        return True
    if tag in (0xC4, 0xC5, 0xC6):
        # A byte payload arrives as an array of integers on this wire. The
        # reference decoder rejects `bin` in the same position, so accepting it
        # here would make lazily-py read frames no conforming peer produces —
        # a private extension wearing the `msgpack` token.
        raise MsgpackCodecError(
            "byte payloads are arrays of integers on this wire, not msgpack `bin`"
        )
    if tag in (0xCA, 0xCB):
        raise MsgpackCodecError("frames carry no floating-point fields")
    if tag == 0xCC:
        return reader.uint(1, ">B")
    if tag == 0xCD:
        return reader.uint(2, ">H")
    if tag == 0xCE:
        return reader.uint(4, ">I")
    if tag == 0xCF:
        return reader.uint(8, ">Q")
    if tag == 0xD0:
        return reader.uint(1, ">b")
    if tag == 0xD1:
        return reader.uint(2, ">h")
    if tag == 0xD2:
        return reader.uint(4, ">i")
    if tag == 0xD3:
        return reader.uint(8, ">q")
    if tag == 0xD9:
        return _unpack_str(reader, reader.uint(1, ">B"))
    if tag == 0xDA:
        return _unpack_str(reader, reader.uint(2, ">H"))
    if tag == 0xDB:
        return _unpack_str(reader, reader.uint(4, ">I"))
    if tag == 0xDC:
        return _unpack_array(reader, reader.uint(2, ">H"))
    if tag == 0xDD:
        return _unpack_array(reader, reader.uint(4, ">I"))
    if tag == 0xDE:
        return _unpack_map(reader, reader.uint(2, ">H"))
    if tag == 0xDF:
        return _unpack_map(reader, reader.uint(4, ">I"))
    raise MsgpackCodecError(f"unsupported MessagePack value in frame: 0x{tag:02x}")


def msgpack_unpack(data: bytes | bytearray | memoryview) -> Any:
    """Parse ``msgpack`` frame bytes back into a ``to_wire()`` value tree.

    Also the *schema-less* view a conformance runner needs: the named-field
    rule is a property of the ENCODING, invisible to any assertion over a
    decoded :class:`~lazily.ipc.IpcMessage`, because a positional encoder
    round-trips every value correctly and is still non-conforming.
    """
    reader = _Reader(bytes(data))
    value = _unpack(reader)
    if reader.pos != len(reader.data):
        raise MsgpackCodecError("trailing bytes after frame")
    return value
