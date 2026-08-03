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

And it classifies the ``key`` slot out of the RAW wire before any decode runs
(``#lznullformblind``). Every key in this fixture's ``expect`` blocks is
byte-identical across the ``omitted`` and ``null`` families — reading an
explicit ``key: null`` as absent IS the leniency — so the four ``null``
scenarios were the four ``omitted`` ones wearing a different id as far as any
post-decode assertion could tell. ``key_form`` came from the fixture's own
label, so the label was trusted rather than checked; the classification now
comes off the ``wire_json`` text and the ``wire_msgpack_hex`` bytes and is
asserted against that label.
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

#: The three wire shapes the clause is about. A form outside this set is a
#: fixture the runner does not understand, and it fails closed rather than
#: falling into whichever branch happens to be last (#lzscenariobodyskip).
_KEY_FORMS = ("omitted", "null", "present")

#: MessagePack ``nil``. The whole distinction under test is one byte wide on
#: the msgpack side: an absent map entry is the ``key`` field never appearing,
#: an explicit null is the field appearing with ``0xc0`` after it.
_MSGPACK_NIL = 0xC0
#: ``fixstr`` of length 3 holding ``key`` — how the field NAME is spelled in a
#: msgpack map, so the byte after it is the slot's value tag.
_MSGPACK_KEY_FIELD = bytes([0xA3]) + b"key"


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


def _node_site(scenario, wire: dict, whose: str) -> dict:
    """The map the scenario's ``key`` slot lives in, inside a plain wire tree.

    Shared by the raw-wire control and the re-encode check so both look at the
    same site — a control that read a different node than the assertion it
    guards would be no control at all.
    """
    if scenario["field"] == "snapshot":
        return wire["Snapshot"]["nodes"][0]
    if scenario["field"] == "node_add":
        return wire["Delta"]["ops"][0]["NodeAdd"]
    # `node_add` used to be the unnamed fallthrough: a scenario naming any other
    # field was read out of the Delta op regardless, and its leniency
    # expectations checked against the wrong node (#lzscenariobodyskip).
    raise AssertionError(f"unknown nodekey field {scenario['field']!r} in {whose}")


def _raw_wire_tree(scenario):
    """The scenario's OWN bytes, parsed schema-lessly, with no decoder in the path.

    ``json.loads`` and :func:`msgpack_unpack` both produce plain dicts, so an
    absent map entry is a key that is not there and an explicit null is a key
    whose value is ``None`` — the one place the two forms are still
    distinguishable. ``IpcMessage.from_wire`` is deliberately NOT called here:
    the typed object collapses them by design, since reading ``key: null`` as
    absent IS the leniency.
    """
    codec = scenario["codec"]
    if codec == "json":
        return json.loads(scenario["wire_json"])
    if codec == "msgpack":
        return msgpack_unpack(bytes.fromhex(scenario["wire_msgpack_hex"]))
    raise AssertionError(f"unknown codec {codec!r}")


def _assert_msgpack_nil_byte(scenario, form: str) -> None:
    """Corroborate a msgpack classification against the raw bytes themselves.

    :func:`msgpack_unpack` is this binding's own decoder, so a defect in it
    would corrupt the control and the thing controlled together. The field name
    ``key`` is a fixstr, so the byte immediately after it is the slot's type
    tag: msgpack ``nil`` (0xc0) is the explicit-null form, anything else is a
    value, and the name not appearing at all is the omitted form. No unpacker is
    involved.
    """
    if scenario["codec"] != "msgpack":
        return
    raw = bytes.fromhex(scenario["wire_msgpack_hex"])
    at = raw.find(_MSGPACK_KEY_FIELD)
    ident = scenario["id"]
    if form == "omitted":
        assert at == -1, (
            f"{ident}: classified `omitted` but the raw msgpack carries a `key` "
            f"field at byte {at}"
        )
        return
    assert at != -1, (
        f"{ident}: classified {form!r} but the raw msgpack carries no `key` field"
    )
    tag = raw[at + len(_MSGPACK_KEY_FIELD)]
    if form == "null":
        assert tag == _MSGPACK_NIL, (
            f"{ident}: classified `null` but the byte after the `key` field is "
            f"{hex(tag)}, not msgpack nil ({hex(_MSGPACK_NIL)})"
        )
    else:
        assert tag != _MSGPACK_NIL, (
            f"{ident}: classified `present` but the `key` slot holds msgpack nil"
        )


def _wire_key_form(scenario) -> str:
    """Classify the ``key`` slot out of the RAW wire, BEFORE any decode runs.

    This control is the whole reason ``wire_encoding`` is dischargeable here.
    Every key in this fixture's ``expect`` blocks is IDENTICAL for the
    ``omitted`` and ``null`` families — ``decoded_key`` is ``None`` for both, by
    design, because that is the leniency under test — so the four ``null``
    scenarios are the four ``omitted`` ones wearing a different id as far as any
    post-decode assertion can tell. A decoder that collapses the two on contact
    satisfies all twelve scenarios while never once distinguishing them, and it
    is invisible to the manifest rung, the scenario-replay rung and both
    assertion-key rungs at once: an unreplayed distinction contributes no
    unconsumed key and no unasserted key. Only a read of the raw slot sees it.
    """
    node = _node_site(scenario, _raw_wire_tree(scenario), "the scenario's own wire")
    if "key" not in node:
        form = "omitted"
    elif node["key"] is None:
        form = "null"
    elif isinstance(node["key"], str):
        form = "present"
    else:
        # Fail closed. A fourth shape — a number, a nested map, a list — is a
        # frame this runner does not understand, and guessing `present` for it
        # would let the corpus grow a form no binding ever classified.
        raise AssertionError(
            f"{scenario['id']}: the raw `key` slot holds {node['key']!r}, which is "
            f"none of the three wire forms {list(_KEY_FORMS)}"
        )
    _assert_msgpack_nil_byte(scenario, form)
    return form


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
    return _node_site(scenario, wire, "the re-encoded frame")


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
        # two" is a claim about the corpus's carriage, not about this run. It is
        # now discharged by the RAW-WIRE CONTROL rather than by a tally of the
        # fixture's own labels: `key_forms` is satisfied only by the three forms
        # `_wire_key_form` read out of each scenario's own bytes — an absent map
        # entry, a JSON `null` / msgpack nil, a string — before any decoder
        # touched them. A runner that re-serialized a pre-parsed object, or a
        # decoder that collapses `null` onto `omitted` on contact, cannot satisfy
        # it. `decoded_key` is the second half: what each classified form decodes
        # to.
        discharged_by=["key_forms", "decoded_key"],
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
        # decode" — `decoded_key` separates them after the decode, `key_forms`
        # separates them BEFORE it (the omitted and null families are otherwise
        # byte-identical in every `expect` block), and `scenario_count` is
        # compared against the frames really replayed.
        discharged_by=["decoded_key", "key_forms", "scenario_count"],
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
        observed_codecs.add(scenario["codec"])

        # THE RAW-WIRE CONTROL, read before the decoder runs. `key_form` used to
        # be taken from the fixture's own label, so the label was trusted rather
        # than checked and a decoder that collapsed `null` onto `omitted` still
        # passed every scenario. The label and the bytes must now agree
        # (`#lznullformblind`).
        declared_form = scenario["key_form"]
        assert declared_form in _KEY_FORMS, (
            f"{scenario['id']}: declares key_form {declared_form!r}, which is none "
            f"of {list(_KEY_FORMS)}"
        )
        on_wire = _wire_key_form(scenario)
        assert declared_form == on_wire, (
            f"{scenario['id']}: scenario declares key_form {declared_form!r} but its "
            f"own wire carries {on_wire!r} — the label and the bytes disagree"
        )
        observed_key_forms.add(on_wire)

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
    # Both directions, against the RAW WIRE rather than against the fixture's
    # own labels: every declared form was carried by a scenario whose own bytes
    # this runner classified before decoding, and no scenario carried a form the
    # block does not declare. A comparison against `scenario["key_form"]` is
    # green over a runner that never opens a frame, and it cannot see `null`
    # collapsing into `omitted` (`#lznullformblind`).
    assert_key_with(
        block,
        "key_forms",
        lambda want: sorted(want) == sorted(observed_key_forms),
        where=f"key forms read off the raw wire {sorted(observed_key_forms)}",
    )

    verify_prose(fixture)
