"""Schema-compliance tests: lazily-py's own serializer output validates against
the lazily-spec JSON Schemas.

This closes the loop between the binding and the canonical schemas: the
``lazily-spec`` drift tests prove fixtures <-> schema, and the conformance tests
prove lazily-py round-trips the fixtures. These tests go one step further and
prove lazily-py's ``to_wire()`` output for binding-constructed messages
(including the NodeKey / CrdtSync surface that the fixtures do not yet cover)
validates against the normative schemas.

Schemas are read through :func:`conformance_assert.schema_json` — the one seam
``LAZILY_SPEC_SCHEMAS_DIR`` moves (``#lzspecschemasoverride``), so a probe can
perturb a schema in a scratch copy instead of dirtying the shared checkout every
other binding reads. They are never read from a vendored copy, which would itself
be a drift hazard.
"""

from __future__ import annotations

import json

import pytest
from conformance_assert import schema_json


jsonschema = pytest.importorskip("jsonschema")
referencing = pytest.importorskip("referencing")
from referencing import Registry  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

from lazily.ipc import (  # noqa: E402
    CausalReceipt,
    CausalReceipts,
    CrdtOp,
    CrdtSync,
    Delta,
    DeltaOp,
    EdgeSnapshot,
    IpcMessage,
    NodeKey,
    NodeSnapshot,
    NodeState_Opaque,
    NodeState_Payload,
    ReceiptOutcome,
    ShmBlobRef,
    Snapshot,
    WireStamp,
)


_SCHEMA_NAMES = ["defs", "snapshot", "delta", "distributed", "receipts"]


def _registry() -> Registry:
    schemas = {
        f"https://lazily.dev/schemas/{name}.json": schema_json(f"{name}.json")
        for name in _SCHEMA_NAMES
    }
    resources = [
        (uri, DRAFT202012.create_resource(schema)) for uri, schema in schemas.items()
    ]
    return Registry().with_resources(resources)


def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    schema = schema_json(f"{schema_name}.json")
    return jsonschema.Draft202012Validator(schema, registry=_registry())


def _assert_valid(wire: object, schema_name: str) -> None:
    errors = sorted(
        _validator(schema_name).iter_errors(wire), key=lambda e: list(e.path)
    )
    assert not errors, (
        f"lazily-py wire output does not validate against {schema_name}.json:\n"
        + "\n".join(f"  - {list(e.path)}: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Snapshot wire output (incl. NodeKey + all NodeState variants)
# ---------------------------------------------------------------------------


def test_snapshot_wire_validates_schema() -> None:
    snap = Snapshot(
        epoch=7,
        nodes=[
            NodeSnapshot.payload(1, "i32", bytes([1, 2, 3])),
            NodeSnapshot.opaque(2, "opaque-type"),
            NodeSnapshot.shared_blob(3, "text/plain", ShmBlobRef(0, 16, 1, 7, 999)),
            NodeSnapshot.payload(4, "i32", bytes([4])).with_key(
                NodeKey.new("scores/alice")
            ),
        ],
        edges=[EdgeSnapshot(2, 1), EdgeSnapshot(3, 1)],
        roots=[1, 2],
    )
    _assert_valid(IpcMessage.of_snapshot(snap).to_wire(), "snapshot")


# ---------------------------------------------------------------------------
# Delta wire output — all 7 op variants + keyed NodeAdd
# ---------------------------------------------------------------------------


def test_delta_wire_validates_schema_all_ops() -> None:
    delta = Delta.next(
        40,
        [
            DeltaOp.cell_set(1, bytes([10])),
            DeltaOp.slot_value(2, bytes([20])),
            DeltaOp.invalidate(3),
            DeltaOp.node_add(
                4, "u64", NodeState_Payload(bytes([64])), NodeKey.new("sheet/A1")
            ),
            DeltaOp.node_add(5, "u8", NodeState_Opaque()),
            DeltaOp.node_remove(6),
            DeltaOp.edge_add(2, 1),
            DeltaOp.edge_remove(3, 1),
        ],
    )
    _assert_valid(IpcMessage.of_delta(delta).to_wire(), "delta")


# ---------------------------------------------------------------------------
# CrdtSync wire output — the third IpcMessage variant (keyed + keyless ops)
# ---------------------------------------------------------------------------


def test_crdt_sync_wire_validates_schema() -> None:
    stamp_a = WireStamp(wall_time=200, logical=0, peer=1)
    stamp_b = WireStamp(wall_time=180, logical=3, peer=2)
    sync = CrdtSync.new(
        [(1, stamp_a), (2, stamp_b)],
        [
            CrdtOp.new(1, stamp_a, bytes([10, 20])),
            CrdtOp.keyed(2, NodeKey.new("scores/alice"), stamp_b, bytes([30])),
        ],
    )
    _assert_valid(IpcMessage.of_crdt_sync(sync).to_wire(), "distributed")


# ---------------------------------------------------------------------------
# Encode/decode bytes path produces schema-valid JSON
# ---------------------------------------------------------------------------


def test_encode_json_bytes_validate_schema() -> None:
    snap = Snapshot(
        epoch=1,
        nodes=[NodeSnapshot.payload(1, "i32", bytes([1, 2, 3, 4]))],
        roots=[1],
    )
    message = IpcMessage.of_snapshot(snap)
    # The byte-encoded transport form, parsed back, is schema-valid.
    wire = json.loads(message.encode_json().decode("utf-8"))
    _assert_valid(wire, "snapshot")


# ---------------------------------------------------------------------------
# CausalReceipts wire output — terminal outcome projection (not a transport ACK)
# ---------------------------------------------------------------------------


def test_causal_receipts_wire_validates_schema() -> None:
    frame = CausalReceipts(
        receipts=[
            CausalReceipt(
                receipt_id="receipt-observed",
                causation_id="patch-123",
                observer="editor",
                generation=7,
                outcome=ReceiptOutcome.OBSERVED,
            ),
            CausalReceipt(
                receipt_id="receipt-applied",
                causation_id="patch-123",
                observer="editor",
                generation=7,
                outcome=ReceiptOutcome.APPLIED,
                payload_hash="sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            ),
            CausalReceipt(
                receipt_id="receipt-rejected",
                causation_id="patch-123",
                observer="editor",
                generation=7,
                outcome=ReceiptOutcome.REJECTED,
                reason="stale generation",
            ),
        ]
    )
    _assert_valid(frame.to_wire(), "receipts")


def test_causal_receipts_encode_json_bytes_validate_schema() -> None:
    frame = CausalReceipts(
        receipts=[
            CausalReceipt(
                receipt_id="r1",
                causation_id="cmd-1",
                observer="worker",
                generation=0,
                outcome=ReceiptOutcome.ACCEPTED,
            )
        ]
    )
    wire = json.loads(frame.encode_json().decode("utf-8"))
    _assert_valid(wire, "receipts")
