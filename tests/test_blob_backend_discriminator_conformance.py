"""Blob-backend discriminator strictness on decode (``#lzblobbackendstrict``).

protocol.md § Shared-memory payload path makes ``ShmBlobRef.backend`` optional
with a default of ``shm``, and that OPTIONALITY is the forward-compatibility
channel: it carries every descriptor minted before the field existed. A PRESENT
value outside the enum is a different fact and gets the opposite answer — the
decoder MUST reject the frame and NAME the token, rather than normalize it to
``shm``, to another backend, or to a sentinel.

lazily-py was one of the five bindings that normalized, and documented the
normalization as forward-compat. The first half of that reasoning was right and
is exactly why absence stays lenient. The second half was not: a new backend
enters the protocol by *adding an enum value* (``docs/zero-copy-transport.md``
§ Pluggable backends — a spec change carrying a fixture), so an unknown token is
a corrupt or non-conforming producer, never a newer peer. And reading an unknown
kind as ``shm`` **is** routing a non-shm descriptor into the shm table, which
inverts the ``resolve_wrong_backend`` theorem in that same doc: it leaves a
64-bit checksum to discharge probabilistically what routing was supposed to
guarantee structurally.

Fixture v2 (14 scenarios: seven wire shapes x two codecs) adds the four facts v1
declared or implied without carrying, and this runner replays all of them:

* ``in_process`` — the THIRD declared backend. v1 shipped scenarios for two of
  the three names in ``assertions.backends``, so a binding that knew only
  ``{shm, arrow}`` refused ``in_process`` *conformingly by the letter of the
  clause* and passed all eight v1 scenarios. The control is a SET DIFFERENCE
  (``declared - decoded``), not a scenario count — see
  :func:`_assert_backend_vocabulary_is_complete`.
* an explicit ``null`` — the ABSENT form (§ NodeKey, ``#lzkeynullstrict``), not a
  present-unknown one. A serde-style peer that skipped ``skip_serializing_if``
  emits ``null`` where a conforming encoder omits, so refusing it is stricter
  than the reference implementation on a frame the reference implementation
  produces.
* a non-string ``backend`` — the same refusal reached through a door the clause,
  written entirely about TOKENS, does not describe. The refusal must arrive
  through the codec's documented decode-error family, so this runner catches
  ``Exception`` and asserts membership rather than pinning the family in a
  ``pytest.raises``: a ``TypeError`` from downstream string handling refuses the
  frame while failing PAST every caller's decode handler, and a
  ``pytest.raises(ValueError)`` cannot tell that apart from a conforming refusal.
* ``frame_epoch`` vs ``blob_epoch`` — v1 carried 9 in both the Delta frame and
  the ShmBlobRef descriptor, so one ``expect.epoch`` was satisfied by a runner
  reading either. They are separate facts (the frame epoch orders deltas, the
  descriptor epoch names the arena incarnation) and are now asserted separately
  against their own sources.

The fixture carries its frames as raw text (json) and hex (msgpack) on purpose.
``schemas/defs.json`` closes ``backend`` to an enum, so the reject frames AND the
null frames are schema-INVALID by design and cannot be carried as parsed objects
— the enum binds a conforming ENCODER, and these frames are what a DECODER must
survive. This runner therefore decodes from the raw form and never re-serializes
a parsed scenario body.

Both halves of the clause are checked. Reading the field is only the decode half
— a binding that echoes whatever it received back out has a correct decoded
value and a non-conforming encoder — so each accepted scenario re-encodes under
its own codec and inspects the produced frame's field set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import (
    TrackedBlock,
    assert_key,
    assert_key_into,
    assert_key_with,
    corpus_fixture,
    fnv1a64_hex,
    instrument,
    prose_key,
    scenarios,
    verify_prose,
)

from lazily.ipc import (
    BlobBackendKind,
    DeltaOp_SlotValue,
    IpcMessage,
    IpcValue_SharedBlob,
)
from lazily.msgpack_codec import msgpack_pack, msgpack_unpack


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"

_FIXTURE = "codec/blob_backend_discriminator.json"

#: The decode-error family this codec documents — the type a caller already
#: guards a decode with. ``lazily.msgpack_codec.MsgpackCodecError`` and
#: ``lazily.ipc.NodeKeyError`` are both ``ValueError`` subclasses, so one
#: ``except ValueError`` around ``IpcMessage.from_wire`` catches every refusal
#: the protocol defines. ``expect.rejection_is_decode_error`` is the assertion
#: that a refusal really arrives here rather than as a ``TypeError`` or
#: ``AttributeError`` escaping from downstream string handling: such a frame is
#: still refused, but it is refused PAST the handler, so the peer never sees the
#: protocol error and never resyncs.
_DECODE_ERROR_FAMILY: type[Exception] = ValueError


def _load() -> dict:
    path = corpus_fixture(_FIXTURE, _LOCAL_FIXTURES / _FIXTURE)
    fixture = instrument(json.loads(path.read_text()), name=_FIXTURE)
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "BlobBackendDiscriminator"
    return fixture


def _wire_input(scenario, expect: TrackedBlock) -> bytes:
    codec = scenario["codec"]
    if codec == "json":
        wire_input = scenario["wire_json"].encode()
    elif codec == "msgpack":
        wire_input = bytes.fromhex(scenario["wire_msgpack_hex"])
    else:
        raise AssertionError(f"unknown codec {codec!r}")
    assert_key(expect, "wire_input_fnv1a64", fnv1a64_hex(wire_input))
    return wire_input


def _wire(scenario, wire_input: bytes) -> Any:
    """Materialise the scenario's wire tree from its RAW carried form.

    Never from a parsed scenario body: the reject and null frames carry a
    ``backend`` the corpus schema does not admit, so they exist in the fixture
    only as text and hex.
    """
    codec = scenario["codec"]
    if codec == "json":
        return json.loads(wire_input)
    if codec == "msgpack":
        return msgpack_unpack(wire_input)
    raise AssertionError(f"unknown codec {codec!r}")


def _decode(scenario, wire_input: bytes) -> IpcMessage:
    codec = scenario["codec"]
    if codec == "json":
        return IpcMessage.decode_json(wire_input.decode())
    if codec == "msgpack":
        return IpcMessage.decode_msgpack(wire_input)
    raise AssertionError(f"unknown codec {codec!r}")


def _wire_blob(wire: Any) -> dict[str, Any]:
    """The SharedBlob map inside a wire tree, decoded or re-encoded."""
    return wire["Delta"]["ops"][0]["SlotValue"]["payload"]["SharedBlob"]


def _blob(message: IpcMessage):
    delta = message.delta
    assert delta is not None, "the fixture declares the Delta variant"
    op = delta.ops[0]
    assert isinstance(op, DeltaOp_SlotValue), "the fixture carries a SlotValue op"
    payload = op.payload
    assert isinstance(payload, IpcValue_SharedBlob), (
        "the fixture carries a SharedBlob payload"
    )
    return delta, op, payload.blob


def _reencoded_blob(scenario, message: IpcMessage) -> dict[str, Any]:
    """Re-encode under the scenario's own codec, then read the field set back.

    Through the wire tree rather than the typed object: the typed object always
    holds a ``BlobBackendKind`` and so cannot distinguish "field omitted"
    from "field written as ``shm``", which is the encoder half under test.
    """
    wire = message.to_wire()
    if scenario["codec"] == "msgpack":
        # Round-trip through the msgpack codec so this asserts what THAT
        # encoder produced, rather than assuming both codecs share `to_wire()`.
        wire = msgpack_unpack(msgpack_pack(wire))
    return _wire_blob(wire)


def _observed_rejection_kind(scenario, wire: Any) -> str:
    """Classify a reject frame from the WIRE, not from the fixture's label.

    ``expect.rejection_kind`` is then asserted against this, so the key is a real
    comparison rather than a value copied out of the fixture and handed back to
    it. The two kinds are refusals of different things — a token outside the enum
    versus a value that is not a token at all — and only the second reaches the
    coercion path where a runtime that reads ``7`` as ``"7"`` normalizes
    silently.
    """
    blob = _wire_blob(wire)
    assert "backend" in blob, (
        f"{scenario['id']}: a reject frame must carry a PRESENT `backend`; "
        f"absence is the lenient form and cannot be refused"
    )
    raw = blob["backend"]
    if not isinstance(raw, str):
        return "non_string"
    return "unknown_token"


def _assert_backend_vocabulary_is_complete(
    block: TrackedBlock, decoded_backends: set[str]
) -> None:
    """Every backend the clause DECLARES must be reachable by a real decode.

    This is the control v1 lacked, and it is a set difference on purpose: the
    fixture declared three backends and carried scenarios for two, so a binding
    implementing a two-member enum refused ``in_process`` — naming the token,
    conformingly — and passed every scenario in the file. No count of scenarios,
    and no assertion over any single scenario, reaches that. Only ``declared -
    decoded`` does.

    The reverse containment is asserted too: a backend this build decodes but the
    clause does not name is a private extension to a wire vocabulary, which is
    the same interoperability failure pointing the other way.
    """
    declared = assert_key_into(
        block,
        "backends",
        lambda fixture_value: fixture_value,
        where="vocabulary completeness",
    )
    missing = sorted(set(declared) - decoded_backends)
    assert not missing, (
        f"backend(s) {missing} are declared in `assertions.backends` but no accept "
        f"scenario decoded to them. Either the binding implements a SMALLER enum "
        f"than the clause declares (it refuses the token, conformingly, and still "
        f"contradicts the protocol), or the fixture declares a backend it does not "
        f"carry — which is exactly how v1 shipped without `in_process`."
    )
    extra = sorted(decoded_backends - set(declared))
    assert not extra, (
        f"decoded backend(s) {extra} are not declared in `assertions.backends`: "
        f"this build accepts a token the wire vocabulary does not define"
    )
    # The library's own enum is the third witness. The set difference above is
    # satisfied by any decoder that reaches all three names; this catches an enum
    # carrying a FOURTH the corpus never exercises, which would be accepted on
    # the wire while no peer knows it.
    implemented = {kind.value for kind in BlobBackendKind}
    assert implemented == set(declared), (
        f"BlobBackendKind implements {sorted(implemented)}, the clause declares "
        f"{sorted(set(declared))}"
    )


def test_blob_backend_discriminator_conformance() -> None:
    fixture = _load()

    block: TrackedBlock = fixture["assertions"]
    assert_key(block, "required_of_binding", "MUST")

    # Prose discharge (#lzprosekeyconvention). `assertions.prose` names the nine
    # keys that state an obligation in English and carry nothing comparable; each
    # is discharged by naming the EXECUTABLE keys this run really asserts. The
    # free-text excuses these replace named the same assertions in a sentence,
    # which was falsifiable in principle and checked by nothing — verify_prose at
    # the bottom of this test is what checks it.
    prose_key(
        block,
        "clause",
        # Both halves: omitted/null/known tokens decode (and do not flatten),
        # unknown-present tokens are refused through the decode-error family and
        # name the token.
        discharged_by=[
            "decoded_backend",
            "rejected",
            "rejection_is_decode_error",
            "error_names_token",
        ],
    )
    prose_key(
        block,
        "wire_encoding",
        discharged_by=["wire_input_fnv1a64"],
    )
    prose_key(
        block,
        "backend_form_vocabulary",
        # "every backend in `assertions.backends` appears as the
        # `decoded_backend` of some accept scenario" — the set difference in
        # _assert_backend_vocabulary_is_complete, plus the seven wire shapes.
        discharged_by=["backends", "backend_forms", "decoded_backend"],
    )
    prose_key(
        block,
        "reject_obligation",
        # "refused for the stated reason", not "refused".
        discharged_by=["error_names_token"],
    )
    prose_key(
        block,
        "null_form",
        # An explicit null is the ABSENT form: it decodes as `shm` and does NOT
        # survive the round trip.
        discharged_by=["decoded_backend", "reencoded_backend_field_present"],
    )
    prose_key(
        block,
        "non_string_form",
        # Refused, through the SAME family as the unknown token, and told apart
        # from it by the kind classified off the wire.
        discharged_by=["rejection_kind", "rejection_is_decode_error"],
    )
    prose_key(
        block,
        "epoch_disambiguation",
        # The two epochs are separate facts carrying different numbers (9, 5),
        # asserted against their own sources.
        discharged_by=["frame_epoch", "blob_epoch"],
    )
    prose_key(
        block,
        "anti_vacuity",
        # The four controls in order: (1)+(2) a real decode that really reads the
        # field, (3) the encoder half, (4) a complete vocabulary — plus the "two
        # codecs are not two implementations" caveat, which is why `codecs` is
        # compared against the codecs actually replayed.
        discharged_by=[
            "decoded_backend",
            "reencoded_backend_field_present",
            "backends",
            "codecs",
            "scenario_count",
        ],
    )
    prose_key(
        block,
        "theorem",
        # PROXY. `resolve_wrong_backend` is a Lean theorem in lazily-formal; a
        # run can only prove its CONSEQUENCE. That consequence is exactly the
        # refusal — normalizing an unknown kind routes rather than refuses — so
        # `rejected` carries it, with `decoded_backend` proving the known kinds
        # are not flattened into one table either.
        discharged_by=["rejected", "decoded_backend"],
    )
    # Anti-vacuity, in the directions the fixture names. `accepted` and
    # `rejected` are the two outcomes, and a runner that decoded nothing would
    # book zero of the first; `decoded_backends` is the set of DISTINCT values a
    # real decode produced, so a decoder that ignores the field and hardcodes
    # `shm` collapses it to one member and fails.
    accepted = 0
    rejected = 0
    decoded_backends: set[str] = set()
    reencode_present = 0
    observed_codecs: set[str] = set()
    observed_outcomes: set[str] = set()
    observed_forms: set[str] = set()
    observed_rejection_kinds: set[str] = set()
    forms_by_codec: dict[str, set[str]] = {}

    for scenario in scenarios(fixture):
        expect: TrackedBlock = scenario["expect"]
        assert scenario["variant"] == "Delta", (
            f"{scenario['id']}: the fixture declares the Delta variant"
        )
        codec = scenario["codec"]
        form = scenario["backend_form"]
        observed_codecs.add(codec)
        observed_outcomes.add(scenario["outcome"])
        observed_forms.add(form)
        forms_by_codec.setdefault(codec, set()).add(form)
        wire_input = _wire_input(scenario, expect)
        wire = _wire(scenario, wire_input)

        if scenario["outcome"] == "reject":
            # Caught as bare `Exception`, NOT as `pytest.raises(ValueError)`.
            # Pinning the family in the raises-context makes
            # `rejection_is_decode_error` unfalsifiable: a stray TypeError from
            # downstream string handling would fail the context manager with
            # "DID NOT RAISE"-shaped noise instead of failing the assertion that
            # names the real defect. The refusal has to be CLASSIFIED, not
            # assumed.
            try:
                _decode(scenario, wire_input)
            except Exception as exc:
                error: Exception = exc
            else:
                raise AssertionError(
                    f"{scenario['id']}: the frame was ACCEPTED. A present "
                    f"`backend` outside the enum must be refused, not normalized "
                    f"to `shm` (#lzblobbackendstrict)"
                )
            rejected += 1
            assert_key(expect, "rejected", True)

            kind = _observed_rejection_kind(scenario, wire)
            observed_rejection_kinds.add(kind)
            assert_key(expect, "rejection_kind", kind)

            # The family assertion. A refusal outside the type every caller
            # already guards a decode with fails PAST the handler: the frame is
            # still refused and the peer still never sees the error.
            assert_key(
                expect,
                "rejection_is_decode_error",
                isinstance(error, _DECODE_ERROR_FAMILY),
                where=f"raised {type(error).__name__}",
            )

            text = str(error)
            if kind == "unknown_token":
                # The discriminating assertion. A decoder that refuses this frame
                # because it mis-parsed `checksum` satisfies a bare is-error
                # check while implementing none of the clause; only the token in
                # the message separates the two.
                token = assert_key_into(
                    expect,
                    "error_names_token",
                    lambda fixture_value: fixture_value,
                )
                assert token in text, (
                    f"{scenario['id']}: the rejection does not name the offending "
                    f"token {token!r} — message was {text!r}"
                )
            else:
                assert "error_names_token" not in expect, (
                    f"{scenario['id']}: a non_string rejection has no token to "
                    f"name; requiring one would pin a message format no codec's "
                    f"native type error carries"
                )
            continue

        assert scenario["outcome"] == "accept", (
            f"{scenario['id']}: unknown outcome {scenario['outcome']!r}"
        )
        message = _decode(scenario, wire_input)
        accepted += 1

        delta, op, blob = _blob(message)
        decoded_backends.add(blob.backend.value)

        # The decode half: absence, an explicit `shm` and an explicit NULL all
        # arrive as SHM, while `arrow` and `in_process` arrive as themselves
        # rather than being flattened.
        assert_key(expect, "decoded_backend", blob.backend.value)

        # The encode half, which no assertion over the decoded value reaches: a
        # conforming encoder OMITS the default, so a pre-field descriptor
        # round-trips byte-identically and a binding cannot satisfy the clause
        # by echoing back whatever it received. It is also where the null form
        # is pinned: an accepted null must not survive the round trip.
        reencoded = _reencoded_blob(scenario, message)
        present = "backend" in reencoded
        if present:
            reencode_present += 1
        assert_key(expect, "reencoded_backend_field_present", present)

        assert_key(expect, "node", op.node)
        assert_key(expect, "offset", blob.offset)
        assert_key(expect, "len", blob.len)
        assert_key(expect, "generation", blob.generation)
        # Two epochs, two sources. The Delta frame's epoch orders deltas; the
        # descriptor's epoch names the arena incarnation the blob was written
        # into. v1 carried 9 in both, so a runner reading either satisfied one
        # `expect.epoch` — that key is GONE, and reading the wrong source now
        # compares 9 against 5.
        assert_key(expect, "frame_epoch", delta.epoch)
        assert_key(expect, "blob_epoch", blob.epoch)
        assert_key(expect, "checksum", blob.checksum)

    assert_key(block, "scenario_count", accepted + rejected)
    assert_key(block, "codecs", sorted(observed_codecs))
    assert_key(block, "outcomes", sorted(observed_outcomes))
    assert_key_with(
        block,
        "backend_forms",
        lambda want: sorted(want) == sorted(observed_forms),
        where=f"replayed forms {sorted(observed_forms)}",
    )
    assert_key_with(
        block,
        "rejection_kinds",
        lambda want: sorted(want) == sorted(observed_rejection_kinds),
        where=f"observed kinds {sorted(observed_rejection_kinds)}",
    )
    _assert_backend_vocabulary_is_complete(block, decoded_backends)

    # The fixture's replay is finished, so the discharge claims above can be
    # checked: the discharged set against `assertions.prose` (which is what
    # consumes that key), and every named key against what this run really
    # asserted (#lzprosekeyconvention).
    verify_prose(fixture)

    assert accepted == 10, (
        f"accepted {accepted} scenarios, want 10: five accepting backend forms "
        f"(omitted, shm, arrow, in_process, null) x two codecs"
    )
    assert rejected == 4, (
        f"rejected {rejected} scenarios, want 4: the `rdma` and non-string frames "
        f"under each codec"
    )
    # Every wire shape is carried under BOTH codecs. Several bindings bridge
    # msgpack into the same DOM the JSON decoder produces, so this proves the
    # bridge and the encoder, NOT two independent discriminator verdicts — see
    # `assertions.anti_vacuity`, which says so in as many words.
    assert forms_by_codec == dict.fromkeys(observed_codecs, observed_forms), (
        f"each backend form must be replayed under every codec; got {forms_by_codec}"
    )
    assert reencode_present == 4, (
        f"re-encoded the field in {reencode_present} scenarios, want 4: only the "
        "`arrow` and `in_process` frames may carry it, and a binding that echoes "
        "the received field back out writes it in six"
    )
