"""Frame-codec round-trip conformance (``#lzmsgpackparity``).

protocol.md § Frame codecs makes ``json`` (the reference codec) and ``msgpack``
(the cross-language binary default) MUST-level for every binding, and requires
every frame to round-trip through both for all three ``IpcMessage`` variants.
That requirement lived only in prose. The four conformance rungs — was the
fixture OPENED, were its keys CONSUMED, were they ASSERTED, was every SCENARIO
replayed — all reason about fixture *content*, and content replay never
exercises a codec, so a binding could carve out a MUST-level codec and stay
green on every rung.

lazily-py implements the ``json`` half. ``msgpack`` is an explicit carve-out
(declared in ``interop_peer.py`` and now in
``scripts/check-conformance-coverage.sh``), so
``codec/frame_roundtrip_msgpack.json`` is listed as known-uncovered rather than
silently ignored.

The runner decodes ``wire``, **re-encodes the decoded message**, decodes again,
and evaluates every ``expect`` key against that second decode. Asserting
against the fixture literal would prove nothing: the literal never passed
through an encoder.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import TrackedBlock, assert_key, instrument, scenarios

from lazily.ipc import IpcMessage, NodeState_Opaque, NodeState_Payload


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"

_JSON_FIXTURE = "codec/frame_roundtrip_json.json"


def _load(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    fixture = instrument(json.loads(path.read_text()), name=name, prose=("note",))
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "FrameCodecRoundTrip"
    return fixture


def _assert_fixture_block(fixture: dict, codec: str, byte_canonical: bool) -> None:
    """Guard the fixture-level block: codec identity plus the two distinct
    senses of "canonical" protocol.md keeps apart (``role`` vs
    ``byte_canonical``). An unread block is the drift ``#lzassertunknownkeys``
    exists to catch."""
    assert fixture["codec"] == codec
    block: TrackedBlock = fixture["assertions"]
    assert_key(block, "codec", codec)
    assert_key(block, "self_describing", True)
    assert_key(block, "byte_canonical", byte_canonical)
    assert_key(block, "required_of_binding", "MUST")
    assert_key(
        block,
        "role",
        "reference" if codec == "json" else "cross_language_binary_default",
    )
    assert_key(block, "scenario_count", len(fixture["scenarios"]))


def _variant(message: IpcMessage) -> str:
    if message.snapshot is not None:
        return "Snapshot"
    if message.delta is not None:
        return "Delta"
    if message.crdt_sync is not None:
        return "CrdtSync"
    raise AssertionError("codec fixture pins no runner for this frame")


def _op_variant(op: object) -> str:
    return type(op).__name__.removeprefix("DeltaOp_")


def _assert_snapshot(block: TrackedBlock, snap) -> None:
    assert_key(block, "epoch", snap.epoch)
    assert_key(block, "node_count", len(snap.nodes))
    assert_key(block, "edge_count", len(snap.edges))
    assert_key(block, "root_count", len(snap.roots))
    assert_key(block, "first_node_type_tag", snap.nodes[0].type_tag)
    state = snap.nodes[0].state
    assert isinstance(state, NodeState_Payload), "first node carries Payload bytes"
    assert_key(block, "first_node_payload", list(state.data))
    opaque = next(n for n in snap.nodes if isinstance(n.state, NodeState_Opaque))
    assert_key(block, "opaque_node_id", opaque.node)
    # The externally-tagged UNIT variant is the shape most likely to decay into
    # ``{"Opaque": null}`` under a re-encode, so name it rather than infer it.
    assert_key(block, "opaque_node_state_tag", opaque.state.to_wire())
    assert_key(block, "first_edge", [snap.edges[0].dependent, snap.edges[0].dependency])
    assert_key(block, "roots", list(snap.roots))


def _assert_delta(block: TrackedBlock, delta) -> None:
    assert_key(block, "base_epoch", delta.base_epoch)
    assert_key(block, "epoch", delta.epoch)
    assert_key(block, "op_count", len(delta.ops))
    assert_key(block, "op_variants", [_op_variant(op) for op in delta.ops])
    assert_key(block, "first_op_payload", list(delta.ops[0].payload.data))
    node_add = next(op for op in delta.ops if _op_variant(op) == "NodeAdd")
    assert_key(block, "node_add_type_tag", node_add.type_tag)


def _assert_crdt_sync(block: TrackedBlock, sync) -> None:
    assert_key(block, "frontier_len", len(sync.frontier))
    assert_key(block, "frontier_first_peer", sync.frontier[0][0])
    assert_key(block, "frontier_first_stamp_wall_time", sync.frontier[0][1].wall_time)
    assert_key(block, "op_count", len(sync.ops))
    assert_key(block, "first_op_node", sync.ops[0].node)
    # Decoded-value assertion, not an encoding one: both codecs WRITE ``key``
    # for a ``CrdtOp`` (null when unset — an anti-entropy op's addressing is
    # part of its merge identity). What must survive the round trip is that the
    # decoder reads that null back as absent.
    assert_key(block, "first_op_key_absent", sync.ops[0].key is None)
    assert_key(block, "second_op_node", sync.ops[1].node)
    assert_key(block, "second_op_key", sync.ops[1].key.to_wire())
    assert_key(block, "second_op_stamp_peer", sync.ops[1].stamp.peer)


def _assert_values(block: TrackedBlock, message: IpcMessage) -> None:
    if message.snapshot is not None:
        _assert_snapshot(block, message.snapshot)
    elif message.delta is not None:
        _assert_delta(block, message.delta)
    else:
        _assert_crdt_sync(block, message.crdt_sync)


def test_json_frames_round_trip() -> None:
    fixture = _load(_JSON_FIXTURE)
    _assert_fixture_block(fixture, "json", byte_canonical=True)

    replayed = 0
    for scenario in scenarios(fixture, name=_JSON_FIXTURE):
        source = IpcMessage.from_wire(scenario["wire"])
        assert _variant(source) == scenario["variant"], (
            "fixture `variant` disagrees with the decoded frame"
        )

        # Encode the DECODED message and decode the result. The fixture literal
        # is never re-asserted, so a codec that silently drops a field cannot be
        # masked by reading the input back.
        encoded = source.encode_json()
        round_tripped = IpcMessage.decode_json(encoded)

        block: TrackedBlock = scenario["expect"]
        assert_key(block, "round_trip_equals_source", round_tripped == source)
        _assert_values(block, round_tripped)
        replayed += 1

    assert replayed == 3, "one scenario per IpcMessage variant"
