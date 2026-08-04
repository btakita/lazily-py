"""Unit tests for the lazily IPC wire types and permission boundary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lazily.ipc import (
    NODE_KEY_MAX_SEGMENTS,
    PROTOCOL_ID,
    PROTOCOL_MAJOR_VERSION,
    SHM_BLOB_HEADER_LEN,
    CapabilityHandshake,
    CrdtOp,
    CrdtSync,
    Delta,
    DeltaOp,
    DeltaOp_SlotValue,
    EdgeSnapshot,
    IpcMessage,
    IpcValue_SharedBlob,
    NodeKey,
    NodeKeyError,
    NodeSnapshot,
    NodeState_Payload,
    OpKind,
    OutboxAck,
    PeerPermissions,
    PermissionDenied,
    RemoteOp,
    ResyncRequest,
    ShmBlobArena,
    ShmBlobCapacityTooSmall,
    ShmBlobChecksumMismatch,
    ShmBlobDescriptorMismatch,
    ShmBlobRef,
    ShmBlobTooLarge,
    Snapshot,
    WireStamp,
)
from lazily.msgpack_codec import MsgpackCodecError, msgpack_pack, msgpack_unpack


# ---------------------------------------------------------------------------
# Round-trip / serialization
# ---------------------------------------------------------------------------


def test_snapshot_round_trip_bytes() -> None:
    snap = Snapshot(
        epoch=7,
        nodes=[
            NodeSnapshot.payload(1, "i32", bytes([1, 2, 3])),
            NodeSnapshot.opaque(2, "opaque-type"),
            NodeSnapshot.shared_blob(3, "text/plain", ShmBlobRef(0, 16, 1, 7, 999)),
        ],
        edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
        roots=[1, 2],
    )
    message = IpcMessage.of_snapshot(snap)
    decoded = IpcMessage.decode_json(message.encode_json())
    assert decoded == message
    assert decoded.snapshot == snap


def test_delta_round_trip_all_ops() -> None:
    delta = Delta.next(
        40,
        [
            DeltaOp.cell_set(1, bytes([10])),
            DeltaOp.slot_value(2, bytes([20])),
            DeltaOp.invalidate(3),
            DeltaOp.node_add(4, "u64", NodeState_Payload(bytes([64]))),
            DeltaOp.node_remove(5),
            DeltaOp.edge_add(2, 1),
            DeltaOp.edge_remove(3, 1),
        ],
    )
    message = IpcMessage.of_delta(delta)
    decoded = IpcMessage.decode_json(message.encode_json())
    assert decoded == message
    assert decoded.delta.epoch == 41


def test_payload_serializes_as_byte_array_not_base64() -> None:
    op = DeltaOp.cell_set(1, bytes([10, 255, 0]))
    assert op.to_wire() == {"CellSet": {"node": 1, "payload": {"Inline": [10, 255, 0]}}}


def test_opaque_serializes_as_bare_string() -> None:
    node = NodeSnapshot.opaque(3, "opaque-type")
    assert node.to_wire()["state"] == "Opaque"


def test_ipc_value_blob_coercion() -> None:
    blob = ShmBlobRef(40, 17, 2, 9, 123)
    op = DeltaOp.slot_value(7, blob)
    assert isinstance(op, DeltaOp_SlotValue)
    assert isinstance(op.payload, IpcValue_SharedBlob)
    assert op.payload.blob == blob


def test_transport_agnostic_bytes() -> None:
    message = IpcMessage.of_delta(
        Delta.next(15, [DeltaOp.cell_set(1, b"cell"), DeltaOp.slot_value(2, b"slot")])
    )
    websocket_text = message.encode_json().decode("utf-8")
    webrtc_data = websocket_text.encode("utf-8")
    ffi_buffer = bytes(webrtc_data)

    assert IpcMessage.decode_json(websocket_text) == message
    assert IpcMessage.decode_json(webrtc_data) == message
    assert IpcMessage.decode_json(ffi_buffer) == message


# ---------------------------------------------------------------------------
# Epoch sequencing
# ---------------------------------------------------------------------------


def test_delta_is_next_after() -> None:
    delta = Delta.next(5, [])
    assert delta.base_epoch == 5
    assert delta.epoch == 6
    assert delta.is_next_after(5)
    assert not delta.is_next_after(4)
    assert not delta.is_next_after(6)


def test_delta_apply_status_apply() -> None:
    delta = Delta.next(5, [])
    status = delta.apply_status(5)
    assert status.is_apply
    assert not status.is_resync_required


def test_delta_apply_status_resync() -> None:
    delta = Delta.new(12, 13, [])
    status = delta.apply_status(10)
    assert status.is_resync_required
    assert (status.last_epoch, status.base_epoch, status.epoch) == (10, 12, 13)


# ---------------------------------------------------------------------------
# Permission boundary
# ---------------------------------------------------------------------------


def test_snapshot_filter_omits_unreadable_nodes() -> None:
    peer_a, peer_b = 1, 2
    perms = PeerPermissions()
    perms.allow_many(peer_a, OpKind.READ, [1, 2])

    snap = Snapshot(
        epoch=5,
        nodes=[
            NodeSnapshot.payload(1, "i32", bytes([1])),
            NodeSnapshot.payload(2, "i32", bytes([2])),
            NodeSnapshot.payload(3, "i32", bytes([3])),
        ],
        edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
        roots=[1, 2, 3],
    )

    filtered = snap.filter_readable(perms, peer_a)
    assert len(filtered.nodes) == 2
    assert len(filtered.edges) == 1  # only 2->1 survives (3 unreadable)
    assert filtered.roots == [1, 2]

    empty = snap.filter_readable(perms, peer_b)
    assert empty.nodes == []
    assert empty.edges == []
    assert empty.roots == []


def test_delta_filter_omits_without_redaction() -> None:
    peer_a = 1
    perms = PeerPermissions()
    perms.allow_many(peer_a, OpKind.READ, [1, 2, 5])

    delta = Delta.next(
        8,
        [
            DeltaOp.cell_set(1, bytes([1])),
            DeltaOp.slot_value(2, bytes([2])),
            DeltaOp.invalidate(3),
            DeltaOp.node_add(4, "u8", NodeState_Payload(bytes([4]))),
            DeltaOp.node_remove(5),
            DeltaOp.edge_add(2, 1),
            DeltaOp.edge_remove(3, 1),
        ],
    )

    filtered = delta.filter_readable(perms, peer_a)
    kinds = [type(op).__name__ for op in filtered.ops]
    assert kinds == [
        "DeltaOp_CellSet",
        "DeltaOp_SlotValue",
        "DeltaOp_NodeRemove",
        "DeltaOp_EdgeAdd",
    ]


def test_permissions_independent_gating() -> None:
    perms = PeerPermissions()
    assert perms.allow(1, RemoteOp.read(10)) is True
    assert perms.allow(1, RemoteOp.read(10)) is False  # already held

    assert perms.is_allowed(1, RemoteOp.read(10))
    # Read grant never implies write or trigger.
    assert not perms.is_allowed(1, RemoteOp.write(10))
    assert not perms.is_allowed(1, RemoteOp.trigger_effect(10))


def test_permissions_check_raises() -> None:
    perms = PeerPermissions()
    perms.allow(1, RemoteOp.read(10))
    perms.check(1, RemoteOp.read(10))  # no raise
    with pytest.raises(PermissionDenied):
        perms.check(1, RemoteOp.write(10))


def test_permissions_revoke_and_prune() -> None:
    perms = PeerPermissions()
    perms.allow(1, RemoteOp.read(10))
    assert perms.peer_count() == 1
    assert perms.revoke(1, RemoteOp.read(10)) is True
    assert perms.revoke(1, RemoteOp.read(10)) is False
    assert perms.peer_count() == 0  # pruned empty peer entry


def test_permissions_revoke_peer() -> None:
    perms = PeerPermissions()
    perms.allow_many(1, OpKind.READ, [10, 11])
    assert perms.revoke_peer(1) is True
    assert perms.revoke_peer(1) is False
    assert perms.peer_count() == 0


def test_filter_readable_preserves_order() -> None:
    perms = PeerPermissions()
    perms.allow_many(1, OpKind.READ, [3, 1])
    assert perms.filter_readable(1, [1, 2, 3, 4]) == [1, 3]
    assert perms.filter_readable(99, [1, 2, 3]) == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_malformed_wire_rejected() -> None:
    with pytest.raises(ValueError):
        IpcMessage.from_wire({"Unknown": {}})
    with pytest.raises(ValueError):
        DeltaOp.from_wire({"Bogus": {}})


# ---------------------------------------------------------------------------
# ShmBlobArena host (parity with lazily-rs / lazily-zig)
# ---------------------------------------------------------------------------


def test_shm_blob_arena_round_trip() -> None:
    arena = ShmBlobArena.with_capacity(256)

    payload = b"hello lazily"
    desc = arena.write_blob(7, payload)

    assert desc.offset == 0
    assert desc.len == len(payload)
    assert desc.epoch == 7
    assert desc.generation == 1

    assert bytes(arena.read_blob(desc)) == payload


def test_shm_blob_arena_rejects_oversized_and_tiny_capacity() -> None:
    arena = ShmBlobArena.with_capacity(SHM_BLOB_HEADER_LEN + 4)
    with pytest.raises(ShmBlobTooLarge):
        arena.write_blob(0, b"abcdef")  # 6 > max_blob_len (4)
    with pytest.raises(ShmBlobCapacityTooSmall):
        ShmBlobArena.with_capacity(SHM_BLOB_HEADER_LEN)


def test_shm_blob_arena_from_buffer_wraps_external_storage() -> None:
    backing = bytearray(128)
    arena = ShmBlobArena.from_buffer(backing)  # aliasing, no copy/zero

    desc = arena.write_blob(1, b"abc")
    assert bytes(arena.read_blob(desc)) == b"abc"
    # the arena writes through into the caller-owned backing buffer
    assert backing[SHM_BLOB_HEADER_LEN : SHM_BLOB_HEADER_LEN + 3] == b"abc"


def test_shm_blob_arena_wraparound_invalidates_stale_descriptor() -> None:
    # capacity holds exactly one max-len blob (header + 5)
    arena = ShmBlobArena.with_capacity(SHM_BLOB_HEADER_LEN + 5)

    first = arena.write_blob(1, b"first")
    assert bytes(arena.read_blob(first)) == b"first"

    # next write wraps to offset 0, bumps generation, overwrites first
    second = arena.write_blob(2, b"2nd!!")
    assert second.offset == 0
    assert second.generation > first.generation

    with pytest.raises(ShmBlobDescriptorMismatch):
        arena.read_blob(first)
    assert bytes(arena.read_blob(second)) == b"2nd!!"


def test_shm_blob_arena_checksum_mismatch_on_corrupted_payload() -> None:
    backing = bytearray(128)
    arena = ShmBlobArena.from_buffer(backing)

    desc = arena.write_blob(0, b"payload")
    backing[SHM_BLOB_HEADER_LEN] ^= 0xFF  # corrupt first payload byte via alias
    with pytest.raises(ShmBlobChecksumMismatch):
        arena.read_blob(desc)


def test_shm_blob_descriptor_flows_through_ipc_value_shared_blob() -> None:
    arena = ShmBlobArena.with_capacity(128)
    desc = arena.write_blob(3, b"blob payload")

    value = IpcValue_SharedBlob(desc)
    assert value.blob == desc
    assert bytes(arena.read_blob(value.blob)) == b"blob payload"


# ---------------------------------------------------------------------------
# NodeKey (wire-stable keyed address)
# ---------------------------------------------------------------------------


def test_node_key_validates_path_bounds() -> None:
    assert NodeKey.new("scores/alice").as_str() == "scores/alice"

    with pytest.raises(NodeKeyError) as exc_empty:
        NodeKey.new("")
    assert exc_empty.value.kind is NodeKeyError.EMPTY

    with pytest.raises(NodeKeyError) as exc_double:
        NodeKey.new("a//b")
    assert exc_double.value.kind is NodeKeyError.EMPTY_SEGMENT

    with pytest.raises(NodeKeyError) as exc_leading:
        NodeKey.new("/leading")
    assert exc_leading.value.kind is NodeKeyError.EMPTY_SEGMENT

    too_many = "/".join(["s"] * (NODE_KEY_MAX_SEGMENTS + 1))
    with pytest.raises(NodeKeyError) as exc_many:
        NodeKey.new(too_many)
    assert exc_many.value.kind is NodeKeyError.TOO_MANY_SEGMENTS

    too_long = "x" * 2000
    with pytest.raises(NodeKeyError) as exc_long:
        NodeKey.new(too_long)
    assert exc_long.value.kind is NodeKeyError.TOO_LONG


def test_node_key_segments_round_trip() -> None:
    key = NodeKey.from_segments(["outer", "k1", "inner", "k2"])
    assert key.as_str() == "outer/k1/inner/k2"
    assert key.segments() == ["outer", "k1", "inner", "k2"]


def test_node_key_wire_is_bare_string() -> None:
    key = NodeKey.new("scores/alice")
    assert key.to_wire() == "scores/alice"
    assert NodeKey.from_wire("scores/alice") == key


def test_keyed_node_snapshot_round_trips_through_json() -> None:
    key = NodeKey.new("scores/alice")
    node = NodeSnapshot.payload(1, "i32", bytes([1])).with_key(key)
    message = IpcMessage.of_snapshot(Snapshot(epoch=1, nodes=[node], roots=[1]))

    encoded = message.encode_json()
    assert b"scores/alice" in encoded
    assert IpcMessage.decode_json(encoded) == message


def test_unkeyed_node_snapshot_omits_key_field() -> None:
    node = NodeSnapshot.payload(1, "i32", bytes([1]))
    message = IpcMessage.of_snapshot(Snapshot(epoch=1, nodes=[node], roots=[1]))
    encoded = message.encode_json().decode("utf-8")
    assert '"key"' not in encoded, f"unkeyed node must omit key field: {encoded}"


def test_node_snapshot_without_key_decodes_to_none() -> None:
    wire = (
        '{"Snapshot":{"epoch":1,"nodes":['
        '{"node":1,"type_tag":"i32","state":{"Payload":[1]}}'
        '],"edges":[],"roots":[1]}}'
    )
    message = IpcMessage.decode_json(wire)
    assert message.snapshot is not None
    assert message.snapshot.nodes[0].key is None


def test_keyed_node_add_delta_round_trips() -> None:
    key = NodeKey.new("sheet/A1")
    delta = Delta.next(
        1,
        [
            DeltaOp.node_add(2, "i32", NodeState_Payload(bytes([2])), key),
            DeltaOp.node_add(3, "i32", NodeState_Payload(bytes([3]))),
        ],
    )
    message = IpcMessage.of_delta(delta)
    encoded = message.encode_json()

    decoded = IpcMessage.decode_json(encoded)
    assert decoded == message
    assert decoded.delta.ops[0].key == key
    assert decoded.delta.ops[1].key is None

    # unkeyed NodeAdd omits key, keyed NodeAdd includes it
    text = encoded.decode("utf-8")
    assert '"key"' in text  # at least one keyed op


def test_unkeyed_node_add_omits_key_field() -> None:
    delta = Delta.next(
        1,
        [DeltaOp.node_add(2, "i32", NodeState_Payload(bytes([2])))],
    )
    text = IpcMessage.of_delta(delta).encode_json().decode("utf-8")
    assert '"key"' not in text, f"unkeyed NodeAdd must omit key field: {text}"


# ---------------------------------------------------------------------------
# Distributed: CRDT cell plane (CrdtSync)
# ---------------------------------------------------------------------------


def test_crdt_sync_round_trips_through_json() -> None:
    stamp_a = WireStamp(wall_time=200, logical=0, peer=1)
    stamp_b = WireStamp(wall_time=180, logical=3, peer=2)
    sync = CrdtSync.new(
        [(1, stamp_a), (2, stamp_b)],
        [
            CrdtOp.new(1, stamp_a, bytes([10, 20])),
            CrdtOp.keyed(2, NodeKey.new("scores/alice"), stamp_b, bytes([30])),
        ],
    )
    message = IpcMessage.of_crdt_sync(sync)
    assert message.is_crdt_sync
    assert message.crdt_sync == sync

    encoded = message.encode_json()
    decoded = IpcMessage.decode_json(encoded)
    assert decoded == message
    assert decoded.crdt_sync == sync
    assert decoded.crdt_sync.ops[1].key == NodeKey.new("scores/alice")


def test_crdt_op_keyless_serializes_null_key() -> None:
    # Byte parity with lazily-rs: a derived struct always carries `key`
    # (null when unset), unlike NodeSnapshot/NodeAdd which omit it.
    op = CrdtOp.new(1, WireStamp(1, 0, 1), bytes([1]))
    assert op.to_wire()["key"] is None


def test_crdt_sync_filter_omits_non_readable_ops_but_keeps_frontier() -> None:
    frontier = [(1, WireStamp(200, 0, 1)), (2, WireStamp(200, 0, 2))]
    sync = CrdtSync.new(
        frontier,
        [
            CrdtOp.new(1, WireStamp(1, 0, 1), bytes([1])),
            CrdtOp.new(2, WireStamp(2, 0, 1), bytes([2])),
            CrdtOp.new(3, WireStamp(3, 0, 1), bytes([3])),
        ],
    )
    perms = PeerPermissions()
    perms.allow_many(1, OpKind.READ, [1, 2])

    filtered = sync.filter_readable(perms, 1)
    assert filtered.frontier == frontier  # frontier kept whole
    assert [op.node for op in filtered.ops] == [1, 2]  # node 3 omitted


def test_crdt_sync_from_wire_accepts_keyless_op() -> None:
    wire = {
        "CrdtSync": {
            "frontier": [[1, {"wall_time": 5, "logical": 0, "peer": 1}]],
            "ops": [
                {
                    "node": 1,
                    "key": None,
                    "stamp": {"wall_time": 5, "logical": 0, "peer": 1},
                    "state": {"Inline": [1]},
                }
            ],
        }
    }
    message = IpcMessage.from_wire(wire)
    assert message.is_crdt_sync
    assert message.crdt_sync.ops[0].key is None
    assert message.to_wire() == wire


# ---------------------------------------------------------------------------
# msgpack codec (#lzmsgpackseven) — the cross-language binary default.
#
# The conformance runner in test_codec_conformance.py replays the canonical
# fixture; these cover the wire rules that fixture cannot reach, because they
# are about frames a CONFORMING peer never sends and this binding must still
# refuse rather than quietly accept.
# ---------------------------------------------------------------------------


def test_msgpack_control_frames_round_trip() -> None:
    # The reliable-sync reverse channel is outside the codec fixture's three
    # scenarios, and an externally tagged unit-ish variant is exactly where a
    # codec silently grows a second shape.
    for message in (
        IpcMessage.of_resync_request(ResyncRequest(from_epoch=3)),
        IpcMessage.of_outbox_ack(OutboxAck(through_epoch=9)),
    ):
        packed = message.encode_msgpack()
        assert IpcMessage.decode_msgpack(packed) == message
        assert sorted(msgpack_unpack(packed)) == sorted(message.to_wire())


def test_msgpack_rejects_bin_in_a_byte_payload_position() -> None:
    # Byte payloads are ARRAYS OF INTEGERS on this wire — what rmp_serde emits
    # for `Vec<u8>` and what its decoder accepts. Reading `bin` here would let
    # lazily-py accept frames no conforming peer produces, which is the private
    # extension the codec token exists to exclude.
    body = msgpack_pack(
        {"Snapshot": {"epoch": 1, "nodes": [], "edges": [], "roots": []}}
    )
    # 0xc4 = bin8; splice one in where the encoder would have written an array.
    frame = body.replace(b"\x90", b"\xc4\x00", 1)
    with pytest.raises(MsgpackCodecError, match="arrays of integers"):
        IpcMessage.decode_msgpack(frame)
    with pytest.raises(MsgpackCodecError, match="arrays of integers"):
        msgpack_pack({"Snapshot": {"payload": b"\x01\x02"}})


def test_msgpack_rejects_non_string_map_keys_and_trailing_bytes() -> None:
    # A map with integer keys is a positional encoding wearing a map's clothes.
    with pytest.raises(MsgpackCodecError, match="string keys"):
        msgpack_unpack(b"\x81\x01\x02")
    with pytest.raises(MsgpackCodecError, match="trailing bytes"):
        msgpack_unpack(msgpack_pack(1) + b"\x01")


def test_msgpack_is_not_byte_canonical_but_is_semantically_stable() -> None:
    # Map key order is encoder-defined (§ Frame codecs), so a peer's frame with
    # a different key order is the SAME frame. Decoding one proves the binding
    # reads the wire rather than only its own emission order.
    snapshot = Snapshot(
        epoch=7,
        nodes=[NodeSnapshot.payload(1, "i32", bytes([1, 2, 3]))],
        edges=[],
        roots=[1],
    )
    message = IpcMessage.of_snapshot(snapshot)
    reordered = msgpack_pack(
        {
            "Snapshot": {
                "roots": [1],
                "edges": [],
                "nodes": [
                    {"state": {"Payload": [1, 2, 3]}, "type_tag": "i32", "node": 1}
                ],
                "epoch": 7,
            }
        }
    )
    assert reordered != message.encode_msgpack()
    assert IpcMessage.decode_msgpack(reordered) == message


# ---------------------------------------------------------------------------
# Capability negotiation
# ---------------------------------------------------------------------------


def test_capability_handshake_defaults() -> None:
    hs = CapabilityHandshake.new(7, "abc-123")
    assert hs.protocol_id == PROTOCOL_ID == "lazily-ipc"
    assert hs.protocol_major_version == PROTOCOL_MAJOR_VERSION == 1
    assert hs.codec == "json"
    assert hs.max_frame_size == 1_048_576
    assert hs.ordered_reliable is True
    assert hs.fragmentation_supported is False
    assert hs.peer_id == 7
    assert hs.session_id == "abc-123"
    assert hs.features == []


def test_capability_handshake_round_trips() -> None:
    hs = (
        CapabilityHandshake.new(1, "s1")
        .with_features(["shared-blob", "signaling-relay"])
        .with_max_frame_size(2_097_152)
        .with_fragmentation(True)
    )
    assert hs.has_feature("shared-blob")
    wire = hs.to_wire()
    assert wire["features"] == ["shared-blob", "signaling-relay"]
    assert CapabilityHandshake.from_wire(wire) == hs


def test_capability_handshake_compatibility() -> None:
    a = CapabilityHandshake.new(1, "s")
    b = CapabilityHandshake.new(2, "s")
    assert a.is_compatible_with(b)
    negotiated = a.negotiate_with(b)
    assert negotiated.compatible is True
    assert negotiated.max_frame_size == 1_048_576
    assert negotiated.fragmentation_supported is False

    # codec mismatch fails closed
    codec_error = a.negotiate_with(b.with_codec("postcard"))
    assert codec_error.compatible is False
    assert codec_error.field == "codec"
    # unordered fails closed
    assert not a.is_compatible_with(
        CapabilityHandshake(
            protocol_id=PROTOCOL_ID,
            protocol_major_version=PROTOCOL_MAJOR_VERSION,
            codec="json",
            max_frame_size=1_048_576,
            ordered_reliable=False,
            peer_id=2,
            session_id="s",
        )
    )
    # wrong protocol id fails closed
    assert not a.is_compatible_with(replace(a, protocol_id="not-lazily"))


def test_capability_handshake_negotiates_frame_limits() -> None:
    local = (
        CapabilityHandshake.new(1, "graph")
        .with_max_frame_size(16 * 1024 * 1024)
        .with_fragmentation(True)
    )
    remote = CapabilityHandshake.new(2, "graph").with_max_frame_size(1024)
    negotiated = local.negotiate_with(remote)
    assert negotiated.compatible is True
    assert negotiated.max_frame_size == 1024
    assert negotiated.fragmentation_supported is False

    both_fragment = remote.with_fragmentation(True)
    negotiated = local.negotiate_with(both_fragment)
    assert negotiated.compatible is True
    assert negotiated.fragmentation_supported is True


def test_capability_handshake_rejects_zero_ceiling_and_invalid_session() -> None:
    valid = CapabilityHandshake.new(1, "graph")

    zero = valid.negotiate_with(
        CapabilityHandshake.new(2, "graph").with_max_frame_size(0)
    )
    assert zero.compatible is False
    assert zero.field == "max_frame_size"

    mismatch = valid.negotiate_with(CapabilityHandshake.new(2, "other"))
    assert mismatch.compatible is False
    assert mismatch.field == "session_id"

    empty = valid.negotiate_with(CapabilityHandshake.new(2, ""))
    assert empty.compatible is False
    assert empty.field == "session_id"
