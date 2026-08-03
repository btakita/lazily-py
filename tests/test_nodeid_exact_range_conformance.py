"""``NodeId`` exact-representation bound (``#lzspecdecoderbound``).

protocol.md § NodeId / PeerId stated the 2^53 bound as a PRODUCER obligation and
said nothing about what a decoder does when it receives a violation. That left
the receiving half undefined, which is exactly where the bindings diverged. The
clause is now normative: **a decoder that cannot represent a received identifier
exactly MUST reject the frame rather than round it.**

lazily-py is the one binding with nothing to refuse. A Python ``int`` is
arbitrary-precision, so ``NodeId`` has no upper bound and every scenario — up to
``u64::MAX`` — decodes exactly. This runner therefore asserts the ``exact``
branch for all six, which makes it a *floor*: a scenario that is rejectable here
means the fixture is wrong about the wire rather than about any decoder.

The fixture carries its wire frames as raw text (json) and hex (msgpack) and its
expected identifier as a decimal string. Python would not round a bare
``9007199254740993`` while loading the file, but the contract is uniform across
the nine bindings on purpose — the ones backed by doubles would, and a fixture
that reads differently per runtime is not one fixture.
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

from lazily.ipc import IpcMessage, NodeState_Payload
from lazily.msgpack_codec import msgpack_unpack


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance"
_SPEC_FIXTURES = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"

_FIXTURE = "codec/nodeid_exact_range.json"


def _load() -> dict:
    spec_path = _SPEC_FIXTURES / _FIXTURE
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / _FIXTURE
    fixture = instrument(json.loads(path.read_text()), name=_FIXTURE)
    assert fixture["protocol_version"] == 1
    assert fixture["kind"] == "NodeIdExactRange"
    return fixture


def _decode(scenario) -> IpcMessage:
    """Decode a scenario's wire frame with the codec it names.

    lazily-py never refuses, so this returns a message rather than a
    result — a raised decode error here is a real failure, not the
    ``exact_or_reject`` branch.
    """
    codec = scenario["codec"]
    if codec == "json":
        return IpcMessage.from_wire(json.loads(scenario["wire_json"]))
    if codec == "msgpack":
        return IpcMessage.from_wire(
            msgpack_unpack(bytes.fromhex(scenario["wire_msgpack_hex"]))
        )
    raise AssertionError(f"unknown codec {codec!r}")


def test_nodeid_exact_range_conformance() -> None:
    fixture = _load()

    block: TrackedBlock = fixture["assertions"]
    assert_key(block, "required_of_binding", "MUST")

    # Prose discharge (#lzprosekeyconvention). Each names the executable keys
    # this run really asserts; verify_prose at the bottom checks the naming.
    prose_key(
        block,
        "clause",
        # "MUST NOT round, truncate, saturate, or wrap": a rounding decoder
        # yields a NEIGHBOURING identifier that still decodes cleanly, so only
        # the decimal rendering sees the substitution. `outcome` is the branch
        # the clause allows a narrower decoder to take.
        discharged_by=["node_id_decimal", "outcome"],
    )
    prose_key(
        block,
        "wire_encoding",
        # "MUST compare the decoded identifier by its decimal rendering" — for
        # the node and for the root, both of which carry the boundary value.
        discharged_by=["node_id_decimal", "root_id_decimal"],
    )
    prose_key(
        block,
        "anti_vacuity",
        # "the two `exact` scenarios are the control ... a binding must prove it
        # decodes the boundary value correctly before its refusals count":
        # `scenario_count` is compared against the number of frames this run
        # really DECODED, not against len(scenarios).
        discharged_by=["scenario_count", "node_id_decimal", "outcome"],
    )
    # NOT prose: `outcomes` maps a vocabulary to English glosses, so the
    # assertion is the KEY SET and the parent key's own assertion discharges it.
    # `generator` is a provenance path with no lazily-py-side value to compare.
    excuse_key(
        block,
        "generator",
        "the fixture's provenance — the script that emitted it lives in "
        "lazily-spec, so there is no lazily-py-side value to compare it to",
    )

    # Anti-vacuity. `exact_or_reject` is satisfied by a runner that decodes
    # nothing, so the count of scenarios this run actually ACCEPTED is the
    # assertion that the decoder ran. Python's range covers the corpus, so the
    # expected value is every scenario — anything less is a narrowing.
    accepted = 0
    observed_codecs: set[str] = set()
    observed_outcomes: set[str] = set()

    for scenario in scenarios(fixture):
        expect: TrackedBlock = scenario["expect"]
        expected = int(expect["node_id_decimal"])

        # `outcome` is the corpus-wide statement of what a decoder may do.
        # lazily-py reads it as a constraint on the fixture: both branches
        # oblige it to decode, because it can represent everything.
        observed_outcomes.add(
            assert_key_with(
                expect,
                "outcome",
                lambda want: want in ("exact", "exact_or_reject"),
            )
        )
        observed_codecs.add(scenario["codec"])

        message = _decode(scenario)
        accepted += 1

        snapshot = message.snapshot
        assert snapshot is not None, (
            f"{scenario['id']}: fixture declares the Snapshot variant"
        )
        assert scenario["variant"] == "Snapshot"

        assert_key(expect, "epoch", snapshot.epoch)
        assert_key(expect, "node_count", len(snapshot.nodes))

        node = snapshot.nodes[0]
        # The discriminating assertion. A rounding decoder yields a NEIGHBOURING
        # identifier that still decodes cleanly and addresses a different node,
        # so comparing the decimal rendering is the only check that sees the
        # substitution.
        assert node.node == expected, (
            f"{scenario['id']}: decoder substituted {node.node} for {expected}"
        )
        assert_key(expect, "node_id_decimal", str(node.node))
        assert_key(expect, "type_tag", node.type_tag)
        state = node.state
        assert isinstance(state, NodeState_Payload), (
            "the fixture carries a Payload node state"
        )
        assert_key(expect, "payload", list(state.data))
        assert len(snapshot.roots) == 1, f"{scenario['id']}: one root"
        assert_key(expect, "root_id_decimal", str(snapshot.roots[0]))

    assert accepted == 6, (
        f"accepted {accepted} scenarios, want 6: a Python int is arbitrary-precision, "
        "so lazily-py has no identifier in this corpus it may refuse"
    )
    # Against what the run DECODED, not against len(fixture["scenarios"]) — the
    # old form compared the fixture to itself and stayed green over a runner that
    # decoded nothing, which is the very vacuity `anti_vacuity` names and now
    # cites this key for.
    assert_key(block, "scenario_count", accepted)
    assert_key(block, "codecs", sorted(observed_codecs))
    # `outcomes` is a vocabulary mapped to English glosses, not a prose key: the
    # assertion is its KEY SET, checked against the outcomes really replayed.
    assert_key_with(
        block,
        "outcomes",
        lambda want: sorted(want) == sorted(observed_outcomes),
        where=f"replayed outcomes {sorted(observed_outcomes)}",
    )

    verify_prose(fixture)
