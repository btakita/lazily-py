"""Frame-codec round-trip conformance (``#lzmsgpackparity``).

protocol.md § Frame codecs makes ``json`` (the reference codec) and ``msgpack``
(the cross-language binary default) MUST-level for every binding, and requires
every frame to round-trip through both for all three ``IpcMessage`` variants.
That requirement lived only in prose. The four conformance rungs — was the
fixture OPENED, were its keys CONSUMED, were they ASSERTED, was every SCENARIO
replayed — all reason about fixture *content*, and content replay never
exercises a codec, so a binding could carve out a MUST-level codec and stay
green on every rung.

lazily-py implements **both** halves (``#lzmsgpackseven``). The ``msgpack``
codec is dependency-free and derived from the same ``to_wire()`` value tree the
``json`` codec serializes, so the externally tagged envelope, the named-field
maps and both ``NodeKey`` rules are identical across the two by construction —
see ``lazily/msgpack_codec.py``.

The runner decodes ``wire``, **re-encodes the decoded message**, decodes again,
and evaluates every ``expect`` key against that second decode. Asserting
against the fixture literal would prove nothing: the literal never passed
through an encoder.

The msgpack half adds one thing the value assertions structurally cannot see.
Named-field encoding is a property of the ENCODING, not of any decoded value: a
positional encoder round-trips every field correctly and is still a private
codec no peer that negotiated ``msgpack`` could read. So the encoded bytes are
also introspected SCHEMA-LESSLY — decoded into a plain map/list tree — and the
envelope key plus the (sorted; map key order is encoder-defined) field names are
asserted against the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conformance_assert import (
    TrackedBlock,
    assert_key,
    instrument,
    prose_key,
    scenarios,
    verify_prose,
)

from lazily.ipc import IpcMessage, NodeState_Opaque, NodeState_Payload
from lazily.msgpack_codec import msgpack_unpack


if TYPE_CHECKING:
    from collections.abc import Callable


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"

_JSON_FIXTURE = "codec/frame_roundtrip_json.json"
_MSGPACK_FIXTURE = "codec/frame_roundtrip_msgpack.json"


def _load(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    # No `prose=("note",)` here any more. These fixtures DECLARE `note` in
    # `assertions.prose`, and a declared key states an obligation rather than
    # annotating one — the blanket by-name exemption is exactly what would let a
    # real rule hide behind a reserved name (#lzprosekeyconvention).
    fixture = instrument(json.loads(path.read_text()), name=name)
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
    # `scenario_count` is NOT asserted here. It is the one key in this block
    # that describes the RUN rather than the codec, and `len(fixture["scenarios"])`
    # is the fixture compared to itself — green over a runner that decodes
    # nothing, which is the exact vacuity `anti_vacuity` exists to name. It is
    # asserted after the replay instead, against the scenarios really replayed
    # (`#lznullformblind`; the same shape was fixed in the nodeid and nodekey
    # runners under `#lzprosekeyconvention` and missed here).

    # `note` is a DECLARED prose key here (#lzprosekeyconvention), not
    # narration. The two fixtures state DIFFERENT obligations under that one
    # name, so they discharge differently rather than sharing a form of words:
    #
    # * json — "`role` is the codec's ROLE ... `byte_canonical` is a separate
    #   property of codec+message ... both are pinned here so a runner cannot
    #   conflate them";
    # * msgpack — "`byte_canonical: false` is the whole reason this fixture pins
    #   decoded values instead of golden bytes ... it is what
    #   `encoded_body_field_names` below verifies".
    prose_key(
        block,
        "note",
        discharged_by=(
            ["role", "byte_canonical"]
            if codec == "json"
            else ["byte_canonical", "encoded_body_field_names"]
        ),
    )


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


def _assert_msgpack_encoding(block: TrackedBlock, variant: str, encoded: bytes) -> None:
    """Assert the *encoding* of a msgpack frame, not its decoded value.

    Everything above this point survives a positional encoder unharmed: an
    encoder that packs each struct as an array round-trips every field and is
    still speaking a private codec that no peer negotiating ``msgpack`` can
    read. The named-field rule is only visible in the bytes, so this decodes
    them schema-lessly — into plain maps and lists, with no ``IpcMessage``
    knowledge in the path — and compares what is actually there.
    """
    raw = msgpack_unpack(encoded)
    assert isinstance(raw, dict), (
        "an `msgpack` frame is an externally tagged map, not "
        f"{type(raw).__name__} — a positional or internally tagged envelope is "
        "a private codec wearing the token"
    )
    assert len(raw) == 1, (
        f"externally tagged envelope carries exactly one entry, got {sorted(raw)}"
    )
    tag, body = next(iter(raw.items()))
    assert_key(block, "encoded_envelope_key", tag)
    assert isinstance(body, dict), "frame bodies are named-field maps"
    # Sorted: a MessagePack map's key order is encoder-defined (§ Frame codecs),
    # so comparing in emission order would pin a property no peer may rely on.
    assert_key(block, "encoded_body_field_names", sorted(body))

    if variant == "Snapshot":
        # No `key`: `NodeSnapshot.key` is absent here and a self-describing
        # codec OMITS it (§ NodeKey). Writing it as `null` instead round-trips
        # identically and breaks the rule that lets a pre-`key` decoder read a
        # post-`key` frame.
        assert_key(block, "first_node_encoded_field_names", sorted(body["nodes"][0]))
    elif variant == "CrdtSync":
        # Both DO carry `key` — a `CrdtOp` always writes it, `null` when unset,
        # because an anti-entropy op's addressing is part of its merge identity.
        assert_key(block, "first_op_encoded_field_names", sorted(body["ops"][0]))
        assert_key(block, "second_op_encoded_field_names", sorted(body["ops"][1]))
    elif variant == "Delta":
        # A `Delta` frame pins no per-element encoded field set: its ops are
        # externally tagged variants, checked as decoded values by
        # `_assert_delta`. Named explicitly rather than left to fall off the end
        # of the chain, so a variant the corpus grows lands in the `else` below
        # instead of silently receiving no encoding assertion at all
        # (#lzscenariobodyskip).
        pass
    else:
        raise AssertionError(f"unknown frame variant {variant!r}")


def _replay(
    fixture: dict,
    name: str,
    encode: Callable[[IpcMessage], Any],
    decode: Callable[[Any], IpcMessage],
    encoding_check: Callable[[TrackedBlock, str, Any], None] | None = None,
) -> int:
    """Decode → RE-ENCODE → decode, asserting only against the second decode.

    One shape serves both codecs because the two fixtures carry identical
    ``wire`` values on purpose; the codec-specific part is the encode/decode
    pair and, for msgpack, the encoding introspection.

    Returns the number of scenarios really replayed, so ``scenario_count`` is
    compared against the run rather than against the fixture's own length.
    """
    replayed = 0
    for scenario in scenarios(fixture, name=name):
        source = IpcMessage.from_wire(scenario["wire"])
        variant = _variant(source)
        assert variant == scenario["variant"], (
            "fixture `variant` disagrees with the decoded frame"
        )

        # Encode the DECODED message and decode the result. The fixture literal
        # is never re-asserted, so a codec that silently drops a field cannot be
        # masked by reading the input back.
        encoded = encode(source)

        block: TrackedBlock = scenario["expect"]
        # The encoding check runs BEFORE the decode on purpose. What is on the
        # wire is the interop obligation; a frame that only this binding's own
        # decoder can read is the failure, and letting the decode go first would
        # let its exception preempt the assertion that names the real defect.
        if encoding_check is not None:
            encoding_check(block, variant, encoded)

        round_tripped = decode(encoded)
        assert_key(block, "round_trip_equals_source", round_tripped == source)
        _assert_values(block, round_tripped)
        replayed += 1

    return replayed


def _assert_scenario_count(fixture: dict, replayed: int) -> None:
    """Pin ``scenario_count`` against the scenarios this run really replayed.

    Not against ``len(fixture["scenarios"])``: that is the fixture compared to
    itself, and it stays green over a runner that opens the file, counts the
    list and decodes nothing — the vacuity the sibling nodeid and nodekey
    runners already closed. A runner that stops replaying a variant now reddens
    here (`#lznullformblind`).
    """
    block: TrackedBlock = fixture["assertions"]
    assert_key(block, "scenario_count", replayed)


def _assert_replay_floor(replayed: int) -> None:
    assert replayed == 3, "one scenario per IpcMessage variant"


def test_json_frames_round_trip() -> None:
    fixture = _load(_JSON_FIXTURE)
    _assert_fixture_block(fixture, "json", byte_canonical=True)
    replayed = _replay(
        fixture,
        _JSON_FIXTURE,
        IpcMessage.encode_json,
        IpcMessage.decode_json,
    )
    _assert_scenario_count(fixture, replayed)
    verify_prose(fixture)
    _assert_replay_floor(replayed)


def test_msgpack_frames_round_trip() -> None:
    """``msgpack`` is MUST-level for every binding (protocol.md § Frame codecs).

    ``byte_canonical=False`` is not a softer obligation, it is a different one:
    two conforming bindings MAY emit byte-different frames for the same
    ``IpcMessage``, so the fixture pins the decoded value plus the encoded field
    NAMES, never a golden byte string.
    """
    fixture = _load(_MSGPACK_FIXTURE)
    _assert_fixture_block(fixture, "msgpack", byte_canonical=False)
    replayed = _replay(
        fixture,
        _MSGPACK_FIXTURE,
        IpcMessage.encode_msgpack,
        IpcMessage.decode_msgpack,
        _assert_msgpack_encoding,
    )
    _assert_scenario_count(fixture, replayed)
    verify_prose(fixture)
    _assert_replay_floor(replayed)
