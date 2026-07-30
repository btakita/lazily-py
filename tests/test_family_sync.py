"""Reactive family-granularity sync conformance (``#lzfamilysync``).

Replays the canonical ``lazily-spec/conformance/familysync`` fixture against the
:class:`~lazily.CrdtPlaneRuntime` family layer — the language-agnostic
conformance every binding MUST validate (``lazily-spec/cell-model.md`` §
"Execution-context flavors", proved in ``lazily-formal`` ``FamilySync.lean``).

A keyed op for a family entry NOT registered locally MATERIALIZES it on ingest
instead of being dropped/mis-addressed: membership propagates, values are
adopted, a later last-writer-wins update converges, re-ingest is idempotent, and
a derived aggregate (count of ``true`` entries) converges across replicas.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import instrument

from lazily import CrdtPlaneRuntime, CrdtSync


_LOCAL_FIXTURES = Path(__file__).resolve().parent / "conformance" / "familysync"
_SPEC_FIXTURES = (
    Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance" / "familysync"
)


def _load_fixture(name: str) -> dict:
    spec_path = _SPEC_FIXTURES / name
    path = spec_path if spec_path.exists() else _LOCAL_FIXTURES / name
    return instrument(json.loads(path.read_text()), name=f"familysync/{name}")


def _suffix_of(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def test_family_sync_materialize_on_ingest() -> None:
    fixture = _load_fixture("materialize_on_ingest.json")
    namespace = fixture["namespace"]
    assert fixture["value_type"] == "bool", "this harness replays the bool value_type"

    for scenario in fixture["scenarios"]:
        name = scenario["name"]

        origin = CrdtPlaneRuntime(scenario["origin_peer"])
        origin.register_family_lww(namespace)

        target = CrdtPlaneRuntime(scenario["target_peer"])
        target.register_family_lww(namespace)
        epoch_before = target.membership_epoch()

        now = 100
        for entry in scenario["origin_sets"]:
            origin.family_set_lww(
                namespace, entry["key"], entry["value"], entry.get("now", now)
            )
            now += 1

        frame = origin.to_sync()
        applied = target.apply_frame(CrdtSync.new(frame.frontier, frame.ops))
        assert applied > 0, f"[{name}] ingest applied at least one op"

        if scenario.get("reingest"):
            reapplied = target.apply_frame(CrdtSync.new(frame.frontier, frame.ops))
            assert reapplied == scenario["expect"]["reingest_applied"], (
                f"[{name}] re-ingest is idempotent"
            )

        expect = scenario["expect"]

        got_keys = sorted(_suffix_of(k) for k in target.family_keys(namespace))
        want_keys = sorted(expect["target_keys"])
        assert got_keys == want_keys, f"[{name}] materialized key set"

        assert len(target.family_keys(namespace)) == expect["target_present_count"], (
            f"[{name}] present count"
        )

        for key, want in expect["target_values"].items():
            assert target.family_value_lww(namespace, key) == want, (
                f"[{name}] value for {key}"
            )

        count_true = sum(
            1
            for k in target.family_keys(namespace)
            if target.family_value_lww(namespace, _suffix_of(k)) is True
        )
        assert count_true == expect["target_count_true"], (
            f"[{name}] derived count of true entries"
        )

        if expect["target_epoch_bumped"]:
            assert target.membership_epoch() != epoch_before, (
                f"[{name}] membership epoch bumped on materialize"
            )


def test_family_set_local_read_back() -> None:
    """A local family set materializes the entry, bumps the epoch, and reads back
    its own converged value (the origin side of the sync)."""
    rt = CrdtPlaneRuntime(1)
    rt.register_family_lww("live")
    assert rt.membership_epoch() == 0
    op = rt.family_set_lww("live", "doc-1", True, 100)
    assert op is not None
    assert rt.membership_epoch() == 1
    assert rt.family_keys("live") == ["live/doc-1"]
    assert rt.family_value_lww("live", "doc-1") is True
    # A later last-writer-wins update converges in place (membership unchanged).
    rt.family_set_lww("live", "doc-1", False, 200)
    assert rt.family_value_lww("live", "doc-1") is False
    assert rt.membership_epoch() == 1


def test_unregistered_namespace_keyed_op_is_not_materialized() -> None:
    """A keyed op whose namespace is NOT a registered family is a plain keyed
    CRDT op — it does not grow family membership or bump the epoch."""
    from lazily import CrdtOp, NodeKey, WireStamp

    rt = CrdtPlaneRuntime(2)
    rt.register_family_lww("live")
    op = CrdtOp.keyed(7, NodeKey.new("scores/alice"), WireStamp(10, 0, 1), bytes([1]))
    assert rt.apply(op) is True
    assert rt.family_keys("live") == []
    assert rt.membership_epoch() == 0
