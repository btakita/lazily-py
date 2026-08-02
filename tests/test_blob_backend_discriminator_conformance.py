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

The fixture carries its frames as raw text (json) and hex (msgpack) on purpose.
``schemas/defs.json`` closes ``backend`` to an enum, so the reject frames are
schema-INVALID by design and cannot be carried as parsed objects — the enum
binds a conforming ENCODER, and these frames are what a DECODER must survive.
This runner therefore decodes from the raw form and never re-serializes a parsed
scenario body.

Both halves of the clause are checked. Reading the field is only the decode half
— a binding that echoes whatever it received back out has a correct decoded
value and a non-conforming encoder — so each accepted scenario re-encodes under
its own codec and inspects the produced frame's field set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance_assert import (
    TrackedBlock,
    assert_key,
    assert_key_with,
    excuse_key,
    instrument,
    scenarios,
)

from lazily.ipc import DeltaOp_SlotValue, IpcMessage, IpcValue_SharedBlob
from lazily.msgpack_codec import msgpack_pack, msgpack_unpack


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"

_FIXTURE = "codec/blob_backend_discriminator.json"


def _load() -> dict:
    spec_path = _SPEC_FIXTURES / _FIXTURE
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / _FIXTURE
    fixture = instrument(json.loads(path.read_text()), name=_FIXTURE)
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "BlobBackendDiscriminator"
    return fixture


def _wire(scenario) -> object:
    """Materialise the scenario's wire tree from its RAW carried form.

    Never from a parsed scenario body: the reject frames carry a ``backend``
    the corpus schema does not admit, so they exist in the fixture only as text
    and hex.
    """
    codec = scenario["codec"]
    if codec == "json":
        return json.loads(scenario["wire_json"])
    if codec == "msgpack":
        return msgpack_unpack(bytes.fromhex(scenario["wire_msgpack_hex"]))
    raise AssertionError(f"unknown codec {codec!r}")


def _blob(message: IpcMessage):
    delta = message.delta
    assert delta is not None, "the fixture declares the Delta variant"
    op = delta.ops[0]
    assert isinstance(op, DeltaOp_SlotValue), "the fixture carries a SlotValue op"
    payload = op.payload
    assert isinstance(payload, IpcValue_SharedBlob), (
        "the fixture carries a SharedBlob payload"
    )
    return op, payload.blob


def _reencoded_blob(scenario, message: IpcMessage) -> dict:
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
    return wire["Delta"]["ops"][0]["SlotValue"]["payload"]["SharedBlob"]


def test_blob_backend_discriminator_conformance() -> None:
    fixture = _load()

    block: TrackedBlock = fixture["assertions"]
    assert_key(block, "required_of_binding", "MUST")
    assert_key(block, "codecs", ["json", "msgpack"])
    assert_key(block, "backends", ["shm", "arrow", "in_process"])
    assert_key(block, "outcomes", ["accept", "reject"])
    assert_key(block, "scenario_count", len(fixture["scenarios"]))
    for prose in (
        "clause",
        "wire_encoding",
        "reject_obligation",
        "anti_vacuity",
        "theorem",
        "generator",
    ):
        excuse_key(
            block,
            prose,
            "prose: it states WHY the fixture is shaped this way; the behaviour it "
            "describes is asserted by the per-scenario decode, re-encode and "
            "rejection below",
        )

    # Anti-vacuity, in the three directions the fixture names. `accepted` and
    # `rejected` are the two outcomes, and a runner that decoded nothing would
    # book zero of the first; `decoded_backends` is the set of DISTINCT values a
    # real decode produced, so a decoder that ignores the field and hardcodes
    # `shm` collapses it to one member and fails.
    accepted = 0
    rejected = 0
    decoded_backends: set[str] = set()
    reencode_present = 0

    for scenario in scenarios(fixture):
        expect: TrackedBlock = scenario["expect"]
        assert scenario["variant"] == "Delta", (
            f"{scenario['id']}: the fixture declares the Delta variant"
        )
        wire = _wire(scenario)

        if scenario["outcome"] == "reject":
            with pytest.raises(ValueError) as excinfo:
                IpcMessage.from_wire(wire)
            rejected += 1
            assert_key(expect, "rejected", True)
            # The discriminating assertion. A decoder that refuses this frame
            # because it mis-parsed `checksum` satisfies a bare is-error check
            # while implementing none of the clause; only the token in the
            # message separates the two.
            text = str(excinfo.value)
            token = assert_key_with(expect, "error_names_token")
            assert token in text, (
                f"{scenario['id']}: the rejection does not name the offending "
                f"token {token!r} — message was {text!r}"
            )
            continue

        assert scenario["outcome"] == "accept", (
            f"{scenario['id']}: unknown outcome {scenario['outcome']!r}"
        )
        message = IpcMessage.from_wire(wire)
        accepted += 1

        op, blob = _blob(message)
        decoded_backends.add(blob.backend.value)

        # The decode half: absence and an explicit `shm` both arrive as SHM,
        # and `arrow` arrives as ARROW rather than being flattened.
        assert_key(expect, "decoded_backend", blob.backend.value)

        # The encode half, which no assertion over the decoded value reaches: a
        # conforming encoder OMITS the default, so a pre-field descriptor
        # round-trips byte-identically and a binding cannot satisfy the clause
        # by echoing back whatever it received.
        reencoded = _reencoded_blob(scenario, message)
        present = "backend" in reencoded
        if present:
            reencode_present += 1
        assert_key(expect, "reencoded_backend_field_present", present)

        assert_key(expect, "node", op.node)
        assert_key(expect, "offset", blob.offset)
        assert_key(expect, "len", blob.len)
        assert_key(expect, "generation", blob.generation)
        assert_key(expect, "epoch", blob.epoch)
        assert_key(expect, "checksum", blob.checksum)

    assert accepted == 6, (
        f"accepted {accepted} scenarios, want 6: three backend forms x two codecs"
    )
    assert rejected == 2, (
        f"rejected {rejected} scenarios, want 2: the `rdma` frame under each codec"
    )
    assert decoded_backends == {"shm", "arrow"}, (
        f"decoded backends {sorted(decoded_backends)}, want ['arrow', 'shm']: a "
        "decoder that discards the discriminator and hardcodes `shm` still passes "
        "the omitted and explicit-shm scenarios, and only this set sees it"
    )
    assert reencode_present == 2, (
        f"re-encoded the field in {reencode_present} scenarios, want 2: only the "
        "`arrow` frames may carry it, and a binding that echoes the received field "
        "back out writes it in four"
    )
