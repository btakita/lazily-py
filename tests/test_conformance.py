"""Cross-language conformance tests for the lazily IPC wire protocol.

Each test loads a canonical JSON fixture and validates that lazily-py agrees on
the wire format. The fixtures are the same files the Rust and Zig bindings test
against, so all implementations stay byte-compatible.

The canonical fixtures live in the sibling ``lazily-spec/conformance`` repo; a
vendored copy under ``tests/conformance`` keeps this binding's standalone CI
self-contained. The spec copy is preferred when present.

Fixture schema::

    {
      "description": "…",
      "protocol_version": 1,
      "kind": "Snapshot" | "Delta",
      "assertions": { … language-agnostic field checks … },
      "wire": { <IpcMessage as serde_json> }
    }
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance_assert import TrackedBlock, assert_key, assert_key_with, instrument

from lazily.ipc import (
    SHM_BLOB_HEADER_LEN,
    BlobBackendKind,
    CausalReceipt,
    CausalReceipts,
    DeltaOp_SlotValue,
    IpcMessage,
    IpcValue_SharedBlob,
    NodeState_Opaque,
    NodeState_Payload,
    NodeState_SharedBlob,
    ReceiptApplyResult,
    ReceiptOutcome,
    ReceiptProjection,
    ShmBlobArena,
)


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"


def load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    fixture = instrument(json.loads(path.read_text()), name=name)
    assert fixture["protocol_version"] == 1, (
        f"fixture {name} uses unsupported protocol version"
    )
    return fixture


def assertions(fixture: dict, name: str) -> TrackedBlock:
    """The fixture's ``assertions`` block, tracked (#lzassertunknownkeys).

    Every key here must be consumed by some test in this suite, and every key
    consumed must reach a comparison against the fixture's own value
    (#lzconsumednotasserted) — read it via :func:`assert_key` /
    :func:`assert_key_with`. A key the runner does not recognise used to fall
    through silently, and a key it read and discarded looked identical to a key it
    checked: the fixture round-tripped, the suite went green, and the field the
    fixture exists for went unchecked.
    """
    block = fixture["assertions"]
    assert isinstance(block, TrackedBlock), f"{name}: assertions block not instrumented"
    return block


def parse_wire(fixture: dict) -> IpcMessage:
    return IpcMessage.from_wire(fixture["wire"])


def variant_name(obj: object) -> str:
    """``DeltaOp_SlotValue`` -> ``SlotValue``: the fixture's language-agnostic tag.

    The canonical fixtures name wire variants the way the schema does. Python
    spells the same variants as ``<Union>_<Variant>`` classes, so the runner can
    compare the fixture's string against the real parsed object rather than
    against a hand-written constant.
    """
    return type(obj).__name__.split("_", 1)[-1]


def assert_round_trip_json(message: IpcMessage, fixture: dict) -> None:
    # Round-trip parity: re-serializing the parsed message yields the same
    # canonical JSON object as the fixture wire.
    assert message.to_wire() == fixture["wire"], (
        f"round-trip JSON mismatch for fixture: {fixture['description']}"
    )
    # And bytes decode back to an equal message.
    assert IpcMessage.decode_json(message.encode_json()) == message


# ---------------------------------------------------------------------------
# Snapshot fixtures
# ---------------------------------------------------------------------------


def test_conformance_snapshot_minimal() -> None:
    fixture = load_fixture("snapshot_minimal.json")
    assert fixture["kind"] == "Snapshot"
    a = assertions(fixture, "snapshot_minimal.json")

    message = parse_wire(fixture)
    assert message.is_snapshot
    snap = message.snapshot

    assert_key(a, "epoch", snap.epoch)
    assert_key(a, "node_count", len(snap.nodes))
    assert_key(a, "edge_count", len(snap.edges))
    assert_key(a, "root_count", len(snap.roots))
    assert_key(a, "first_node_type_tag", snap.nodes[0].type_tag)
    assert isinstance(snap.nodes[0].state, NodeState_Payload)

    assert_round_trip_json(message, fixture)


def test_conformance_snapshot_multi_node() -> None:
    fixture = load_fixture("snapshot_multi_node.json")
    assert fixture["kind"] == "Snapshot"
    a = assertions(fixture, "snapshot_multi_node.json")

    message = parse_wire(fixture)
    snap = message.snapshot
    assert_key(a, "epoch", snap.epoch)
    assert_key(a, "node_count", len(snap.nodes))
    assert_key(a, "edge_count", len(snap.edges))
    assert_key(a, "root_count", len(snap.roots))

    opaque_nodes = [n for n in snap.nodes if isinstance(n.state, NodeState_Opaque)]
    assert len(opaque_nodes) == 1, "fixture names exactly one opaque node"
    assert_key(a, "opaque_node_id", opaque_nodes[0].node)
    assert_key(a, "has_opaque_node", bool(opaque_nodes))

    assert_round_trip_json(message, fixture)


def test_conformance_snapshot_shared_blob() -> None:
    fixture = load_fixture("snapshot_shared_blob.json")
    assert fixture["kind"] == "Snapshot"
    a = assertions(fixture, "snapshot_shared_blob.json")

    message = parse_wire(fixture)
    snap = message.snapshot
    assert_key(a, "epoch", snap.epoch)
    assert_key(a, "node_count", len(snap.nodes))
    assert_key(a, "edge_count", len(snap.edges))
    assert_key(a, "root_count", len(snap.roots))

    state = snap.nodes[0].state
    assert isinstance(state, NodeState_SharedBlob)
    assert_key(a, "first_node_state_kind", variant_name(state))
    assert_key(a, "blob_offset", state.blob.offset)
    assert_key(a, "blob_len", state.blob.len)
    assert_key(a, "blob_epoch", state.blob.epoch)

    assert_round_trip_json(message, fixture)


# ---------------------------------------------------------------------------
# Delta fixtures
# ---------------------------------------------------------------------------


def test_conformance_delta_sequential() -> None:
    fixture = load_fixture("delta_sequential.json")
    assert fixture["kind"] == "Delta"
    a = assertions(fixture, "delta_sequential.json")

    message = parse_wire(fixture)
    assert message.is_delta
    delta = message.delta

    base_epoch = assert_key(a, "base_epoch", delta.base_epoch)
    assert_key(a, "epoch", delta.epoch)
    assert_key(a, "is_sequential", delta.is_next_after(base_epoch))
    assert not delta.is_next_after(base_epoch - 1)

    assert_key(a, "op_count", len(delta.ops))

    seen = {type(op).__name__ for op in delta.ops}
    all_variants = {
        "DeltaOp_CellSet",
        "DeltaOp_SlotValue",
        "DeltaOp_Invalidate",
        "DeltaOp_NodeAdd",
        "DeltaOp_NodeRemove",
        "DeltaOp_EdgeAdd",
        "DeltaOp_EdgeRemove",
    }
    assert_key(a, "has_all_op_variants", seen == all_variants)
    assert len(seen) == 7

    assert_round_trip_json(message, fixture)


def test_conformance_delta_non_sequential() -> None:
    fixture = load_fixture("delta_non_sequential.json")
    assert fixture["kind"] == "Delta"
    a = assertions(fixture, "delta_non_sequential.json")

    message = parse_wire(fixture)
    delta = message.delta
    base_epoch = assert_key(a, "base_epoch", delta.base_epoch)
    epoch = assert_key(a, "epoch", delta.epoch)
    # `is_sequential` is a property of the frame (epoch == base_epoch + 1), not
    # of any particular receiver; the gap below is what makes it need a resync.
    assert_key(a, "is_sequential", delta.is_next_after(base_epoch))
    assert not delta.is_next_after(10)

    status = delta.apply_status(10)
    assert_key(a, "resync_after_epoch_10", status.is_resync_required)
    assert status.last_epoch == 10
    assert status.base_epoch == base_epoch
    assert status.epoch == epoch

    assert_round_trip_json(message, fixture)


def test_conformance_delta_shared_blob() -> None:
    fixture = load_fixture("delta_shared_blob.json")
    assert fixture["kind"] == "Delta"
    a = assertions(fixture, "delta_shared_blob.json")

    message = parse_wire(fixture)
    delta = message.delta
    assert_key(a, "base_epoch", delta.base_epoch)
    assert_key(a, "epoch", delta.epoch)
    assert_key(a, "op_count", len(delta.ops))

    op = delta.ops[0]
    assert isinstance(op, DeltaOp_SlotValue)
    assert_key(a, "first_op_kind", variant_name(op))
    assert isinstance(op.payload, IpcValue_SharedBlob)
    assert_key(a, "first_op_payload_kind", variant_name(op.payload))
    assert op.payload.blob.offset == 40
    assert op.payload.blob.len == 17
    assert op.payload.blob.epoch == 9
    # A `backend`-absent descriptor defaults to shm (backward compatibility).
    assert op.payload.blob.backend is BlobBackendKind.SHM

    assert_round_trip_json(message, fixture)


def test_conformance_delta_zero_copy_arrow() -> None:
    # Zero-copy transport (#lzzcpy): the SharedBlob descriptor carries the
    # optional `backend` discriminator selecting a pluggable backend.
    fixture = load_fixture("delta_zero_copy_arrow.json")
    assert fixture["kind"] == "Delta"
    a = assertions(fixture, "delta_zero_copy_arrow.json")

    message = parse_wire(fixture)
    delta = message.delta
    assert_key(a, "base_epoch", delta.base_epoch)
    assert_key(a, "epoch", delta.epoch)
    assert_key(a, "op_count", len(delta.ops))

    op = delta.ops[0]
    assert isinstance(op, DeltaOp_SlotValue)
    assert_key(a, "first_op_kind", variant_name(op))
    assert isinstance(op.payload, IpcValue_SharedBlob)
    assert_key(a, "first_op_payload_kind", variant_name(op.payload))
    assert op.payload.blob.backend is BlobBackendKind.ARROW
    assert_key(a, "first_op_payload_backend", op.payload.blob.backend.value)

    # The `backend` discriminator survives a JSON round-trip byte-for-byte.
    assert_round_trip_json(message, fixture)


# ---------------------------------------------------------------------------
# ShmBlobArena host fixture (not a wire type — locks the arena byte contract)
# ---------------------------------------------------------------------------


def test_conformance_arena_blob() -> None:
    fixture = load_fixture("arena_blob.json")
    assert fixture["kind"] == "Arena"
    a = assertions(fixture, "arena_blob.json")

    arena = ShmBlobArena.with_capacity(fixture["input"]["capacity"])
    payload = bytes(fixture["input"]["payload"])
    desc = arena.write_blob(fixture["input"]["epoch"], payload)

    # Every one of these compares the fixture's value against the arena the
    # binding really built, not against the fixture's own `input` block.
    assert_key(a, "capacity", arena.capacity)
    assert_key(a, "epoch", desc.epoch)
    assert_key(a, "payload_len", desc.len)

    written = {
        "offset": desc.offset,
        "len": desc.len,
        "generation": desc.generation,
        "epoch": desc.epoch,
        "checksum": desc.checksum,
    }
    # The `assertions` copy of the descriptor is the language-agnostic one and the
    # `expected` copy is the byte-level one; both must describe the descriptor the
    # arena actually returned, which also proves the fixture agrees with itself.
    assert_key(a, "descriptor", written)
    expected = fixture["expected"]
    assert_key(expected, "descriptor", written)

    # 40-byte LZSH header byte-identical across rs / py / zig
    buf = arena.buffer()
    header_len = assert_key(a, "header_len", SHM_BLOB_HEADER_LEN)
    # `magic` is the u32 0x4C5A5348 spelled as its ASCII reading; the header
    # stores it little-endian, so the bytes on the wire are reversed.
    assert_key(a, "magic", bytes(buf[0:4])[::-1].decode("ascii"))
    assert_key_with(
        expected,
        "header_bytes",
        lambda want: bytes(buf[0:header_len]) == bytes(want),
    )
    assert_key_with(
        expected,
        "payload_region",
        lambda want: bytes(buf[header_len : header_len + len(payload)]) == bytes(want),
    )

    # round-trip
    assert bytes(arena.read_blob(desc)) == payload


# ---------------------------------------------------------------------------
# Every fixture round-trips, parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "snapshot_minimal.json",
        "snapshot_multi_node.json",
        "snapshot_shared_blob.json",
        "delta_sequential.json",
        "delta_non_sequential.json",
        "delta_shared_blob.json",
        "delta_zero_copy_arrow.json",
    ],
)
def test_fixture_round_trips(name: str) -> None:
    fixture = load_fixture(name)
    message = parse_wire(fixture)
    assert message.to_wire() == fixture["wire"]
    assert IpcMessage.decode_json(message.encode_json()) == message


# ---------------------------------------------------------------------------
# Causal receipt fixture (outcome projection — NOT a transport ACK)
# ---------------------------------------------------------------------------


def test_conformance_causal_receipts() -> None:
    fixture = load_fixture("receipts/causal_receipts.json")
    assert fixture["kind"] == "Receipt"
    assert fixture["model"] == "CausalReceipt"
    a = assertions(fixture, "receipts/causal_receipts.json")

    frame = CausalReceipts.from_wire(fixture["wire"])
    assert_key(a, "receipt_count", len(frame.receipts))

    # Round-trip parity: re-serializing the parsed frame yields the same wire.
    assert frame.to_wire() == fixture["wire"]
    assert CausalReceipts.decode_json(frame.encode_json()) == frame

    # The frame is not an IpcMessage variant; it is its own externally-tagged
    # envelope, and survives a bytes round-trip.
    assert set(frame.to_wire().keys()) == {"CausalReceipts"}

    groups = frame.group_by_causation()
    (causation,) = groups.keys()
    assert_key(a, "causation_id", causation)

    # Default authority = max generation seen among the matching receipts,
    # which makes the older generation stale (the fixture's invariant).
    projection = ReceiptProjection.from_receipts(causation, frame.receipts)
    assert_key(a, "current_generation", projection.current_generation)
    assert projection.terminal_outcome is ReceiptOutcome.APPLIED
    assert_key(a, "terminal_outcome", projection.terminal_outcome.value)
    assert projection.is_terminal
    assert not projection.in_conflict
    assert_key(a, "stale_receipt_ids", projection.stale_receipt_ids())
    assert projection.nonterminal_outcomes() == [
        ReceiptOutcome.OBSERVED,
        ReceiptOutcome.ACCEPTED,
    ]
    assert_key(
        a,
        "nonterminal_outcomes",
        [o.value for o in projection.nonterminal_outcomes()],
    )

    # The fixture orders receipts observed -> accepted -> applied -> stale.
    observed, accepted, applied, stale = frame.receipts
    assert observed.outcome is ReceiptOutcome.OBSERVED
    assert accepted.outcome is ReceiptOutcome.ACCEPTED
    assert applied.outcome is ReceiptOutcome.APPLIED
    assert applied.payload_hash is not None
    assert applied.payload_hash.startswith("sha256:")
    assert stale.outcome is ReceiptOutcome.REJECTED
    assert stale.generation < projection.current_generation


def test_receipt_projection_apply_kernel_matches_formal_model() -> None:
    """Replays LazilyFormal.Receipt.apply named theorems as concrete cases."""
    base = CausalReceipt(
        receipt_id="r",
        causation_id="c",
        observer="o",
        generation=3,
        outcome=ReceiptOutcome.OBSERVED,
    )

    # duplicate_receipt_noop — same receipt_id is a no-op regardless of body.
    proj = ReceiptProjection("c", 3)
    assert proj.apply(base) is ReceiptApplyResult.RECORDED
    dup = CausalReceipt(
        receipt_id="r",
        causation_id="c",
        observer="o",
        generation=3,
        outcome=ReceiptOutcome.REJECTED,
    )
    assert proj.apply(dup) is ReceiptApplyResult.DUPLICATE
    assert proj.terminal_outcome is None  # duplicate did not flip state

    # stale_generation_discarded — older generation is ignored.
    proj = ReceiptProjection("c", 3)
    stale = CausalReceipt(
        receipt_id="r2",
        causation_id="c",
        observer="o",
        generation=2,
        outcome=ReceiptOutcome.APPLIED,
    )
    assert proj.apply(stale) is ReceiptApplyResult.STALE_GENERATION
    assert proj.terminal_outcome is None
    assert proj.stale_receipt_ids() == ["r2"]

    # nonterminal_records_without_terminal_conflict.
    proj = ReceiptProjection("c", 3)
    nt = CausalReceipt(
        receipt_id="r3",
        causation_id="c",
        observer="o",
        generation=3,
        outcome=ReceiptOutcome.ACCEPTED,
    )
    assert proj.apply(nt) is ReceiptApplyResult.RECORDED
    assert proj.terminal_outcome is None

    # first_terminal_records.
    proj = ReceiptProjection("c", 3)
    term = CausalReceipt(
        receipt_id="r4",
        causation_id="c",
        observer="o",
        generation=3,
        outcome=ReceiptOutcome.APPLIED,
    )
    assert proj.apply(term) is ReceiptApplyResult.RECORDED
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED

    # distinct_terminal_conflicts — second different terminal outcome fails closed.
    other = CausalReceipt(
        receipt_id="r5",
        causation_id="c",
        observer="o",
        generation=3,
        outcome=ReceiptOutcome.REJECTED,
    )
    assert proj.apply(other) is ReceiptApplyResult.TERMINAL_CONFLICT
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED  # unchanged
    assert proj.in_conflict
    assert proj.conflicting_receipt_ids() == ["r5"]


def test_receipt_projection_same_terminal_outcome_is_idempotent() -> None:
    """A second terminal receipt with the SAME outcome re-records without conflict."""
    proj = ReceiptProjection("c", 1)
    first = CausalReceipt("a", "c", "o", 1, ReceiptOutcome.APPLIED)
    second = CausalReceipt("b", "c", "o", 1, ReceiptOutcome.APPLIED)
    assert proj.apply(first) is ReceiptApplyResult.RECORDED
    assert proj.apply(second) is ReceiptApplyResult.RECORDED
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED
    assert not proj.in_conflict


def test_receipt_projection_ignores_other_causation_ids() -> None:
    proj = ReceiptProjection.from_receipts(
        "c1",
        [
            CausalReceipt("r1", "c1", "o", 5, ReceiptOutcome.APPLIED),
            CausalReceipt("r2", "c2", "o", 5, ReceiptOutcome.REJECTED),
        ],
    )
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED
    assert proj.current_generation == 5


def test_causal_receipts_frame_round_trips() -> None:
    fixture = load_fixture("receipts/causal_receipts.json")
    frame = CausalReceipts.from_wire(fixture["wire"])
    assert frame.to_wire() == fixture["wire"]
    assert CausalReceipts.decode_json(frame.encode_json()) == frame
