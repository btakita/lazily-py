"""``NodeKey`` null-leniency on decode (``#lzkeynullstrict``).

protocol.md § NodeKey said a self-describing codec OMITS an absent ``key``, and
that a decoder seeing no ``key`` field treats it as absent. That settled the
omitted form and left an explicit ``key: null`` undefined — and three bindings
diverged there. The clause is now explicit: **omit-when-absent binds the
ENCODER, and a decoder MUST accept both forms as absent**, refusing neither and
constructing a key from neither.

lazily-py was one of the three that refused. ``"key" in d`` is true for the null
form, so ``None`` reached ``NodeKey.from_wire`` and raised ``NodeKeyError`` —
"node key path is empty". Note where it was already right: ``CrdtOp.from_wire``
one field over used ``d.get("key")`` and compared against ``None``, because a
``CrdtOp`` ALWAYS writes ``key: null`` when unset. Same file, same field name,
opposite behaviour.

The runner checks both halves. Reading the null form as absent is only half the
rule — a binding that writes it straight back out has a correct decoded value
and a non-conforming encoder — so each scenario re-encodes under its own codec
and inspects the produced frame's field set.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import (
    TrackedBlock,
    assert_key,
    assert_key_with,
    excuse_key,
    instrument,
    prose_key,
    scenarios,
    verify_prose,
)

from lazily.ipc import DeltaOp_NodeAdd, IpcMessage
from lazily.msgpack_codec import msgpack_pack, msgpack_unpack


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"

_FIXTURE = "codec/nodekey_null_leniency.json"


def _load() -> dict:
    spec_path = _SPEC_FIXTURES / _FIXTURE
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / _FIXTURE
    fixture = instrument(json.loads(path.read_text()), name=_FIXTURE)
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "NodeKeyNullLeniency"
    return fixture


def _decode(scenario) -> IpcMessage:
    codec = scenario["codec"]
    if codec == "json":
        return IpcMessage.from_wire(json.loads(scenario["wire_json"]))
    if codec == "msgpack":
        return IpcMessage.from_wire(
            msgpack_unpack(bytes.fromhex(scenario["wire_msgpack_hex"]))
        )
    raise AssertionError(f"unknown codec {codec!r}")


def _reencoded_node(scenario, message: IpcMessage) -> dict:
    """Re-encode under the scenario's own codec, then read the field set back.

    Through the wire tree rather than the typed object, because the typed object
    cannot distinguish "field absent" from "field present and null" — which is
    the entire distinction under test.
    """
    wire = message.to_wire()
    if scenario["codec"] == "msgpack":
        # Round-trip through the msgpack codec so this asserts what THAT encoder
        # produced. Both codecs derive from the same `to_wire()` tree here, but
        # that is a property worth proving rather than assuming: the
        # `#lzmsgpackparity` defect was a msgpack encoder writing `key: null`
        # while json omitted it.
        wire = msgpack_unpack(msgpack_pack(wire))

    if scenario["field"] == "snapshot":
        return wire["Snapshot"]["nodes"][0]
    if scenario["field"] == "node_add":
        return wire["Delta"]["ops"][0]["NodeAdd"]
    # `node_add` used to be the unnamed fallthrough: a scenario naming any other
    # field was read out of the Delta op regardless, and its leniency
    # expectations checked against the wrong node (#lzscenariobodyskip).
    raise AssertionError(f"unknown nodekey field {scenario['field']!r}")


def _decoded_key(scenario, message: IpcMessage) -> str | None:
    if scenario["field"] == "snapshot":
        key = message.snapshot.nodes[0].key
    else:
        op = message.delta.ops[0]
        assert isinstance(op, DeltaOp_NodeAdd), "the fixture declares a NodeAdd op"
        key = op.key
    return None if key is None else key.to_wire()


def test_nodekey_null_leniency_conformance() -> None:
    fixture = _load()

    block: TrackedBlock = fixture["assertions"]
    assert_key(block, "required_of_binding", "MUST")

    # Prose discharge (#lzprosekeyconvention). Each names the executable keys
    # this run really asserts; verify_prose at the bottom checks the naming.
    prose_key(
        block,
        "clause",
        # "accept both forms as absent, refusing neither and constructing a key
        # from neither" is the decode half; "omit-when-absent binds the ENCODER"
        # is the re-encode half, which no assertion over a decoded value reaches.
        discharged_by=["decoded_key", "reencoded_key_field_present"],
    )
    prose_key(
        block,
        "wire_encoding",
        # PROXY. "A pre-parsed object cannot express the difference between the
        # two" is a claim about the corpus's carriage, not about this run. The
        # closest executable evidence that the distinction survived: the three
        # wire forms are compared against the forms really replayed, under both
        # codecs, and each form's decoded key is asserted separately — so a
        # runner that lost the omitted/null distinction reddens.
        discharged_by=["key_forms", "decoded_key", "codecs"],
    )
    prose_key(
        block,
        "reencode_obligation",
        # "A runner MUST re-encode the decoded message and inspect the resulting
        # frame for the presence of the field."
        discharged_by=["reencoded_key_field_present"],
    )
    prose_key(
        block,
        "anti_vacuity",
        # "`present` forces a real key through and `omitted` forces a real
        # decode" — `decoded_key` is the only key that separates them, and
        # `scenario_count` is compared against the frames really replayed.
        discharged_by=["decoded_key", "scenario_count"],
    )
    # NOT prose: a provenance path with no lazily-py-side value to compare.
    excuse_key(
        block,
        "generator",
        "the fixture's provenance — the script that emitted it lives in "
        "lazily-spec, so there is no lazily-py-side value to compare it to",
    )

    # Anti-vacuity in both directions. A runner that never decodes reports
    # "absent" for everything and satisfies all eight omitted/null scenarios; the
    # `present` count is what only a real decode can produce.
    keys_decoded = 0
    replayed = 0
    observed_fields: set[str] = set()
    observed_key_forms: set[str] = set()
    observed_codecs: set[str] = set()

    for scenario in scenarios(fixture):
        expect: TrackedBlock = scenario["expect"]
        replayed += 1
        observed_fields.add(scenario["field"])
        observed_key_forms.add(scenario["key_form"])
        observed_codecs.add(scenario["codec"])

        message = _decode(scenario)
        key = _decoded_key(scenario, message)
        if key is not None:
            keys_decoded += 1

        # The decode half: omitted and explicit-null must both arrive absent.
        assert_key(expect, "decoded_key", key)

        node = _reencoded_node(scenario, message)
        # The encode half, which no assertion over the decoded value can reach.
        assert_key(expect, "reencoded_key_field_present", node.get("key") is not None)

        assert_key(expect, "node", node["node"])
        assert_key(expect, "type_tag", node["type_tag"])
        assert_key(expect, "payload", list(node["state"]["Payload"]))
        epoch = (
            message.snapshot.epoch
            if message.snapshot is not None
            else message.delta.epoch
        )
        assert_key(expect, "epoch", epoch)

    assert replayed == 12, "two fields x three key forms x two codecs"
    assert keys_decoded == 4, (
        f"decoded {keys_decoded} keys, want 4: only the `present` scenarios carry one, "
        "so a runner reporting absent for everything satisfies the null cases trivially"
    )

    # Against what the run REPLAYED, not against hand-written literals and not
    # against len(fixture["scenarios"]) — the old forms compared the fixture to a
    # constant or to itself, so a runner that stopped replaying a wire form or a
    # field stayed green on exactly the keys `anti_vacuity` and `wire_encoding`
    # now cite.
    assert_key(block, "scenario_count", replayed)
    assert_key_with(
        block,
        "codecs",
        lambda want: sorted(want) == sorted(observed_codecs),
        where=f"replayed codecs {sorted(observed_codecs)}",
    )
    assert_key_with(
        block,
        "fields",
        lambda want: sorted(want) == sorted(observed_fields),
        where=f"replayed fields {sorted(observed_fields)}",
    )
    assert_key_with(
        block,
        "key_forms",
        lambda want: sorted(want) == sorted(observed_key_forms),
        where=f"replayed key forms {sorted(observed_key_forms)}",
    )

    verify_prose(fixture)
