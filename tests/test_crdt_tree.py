"""Canonical ``CrdtTree`` algebra fixture replay (``#lzcrdttree``)."""

from __future__ import annotations

import json
from pathlib import Path

from conformance_assert import assert_key, instrument

from lazily import CrdtTree, TextCrdt


_LOCAL_FIXTURE = (
    Path(__file__).resolve().parent / "conformance" / "crdt-tree" / "algebra.json"
)
_SPEC_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "lazily-spec"
    / "conformance"
    / "crdt-tree"
    / "algebra.json"
)


def _fixture() -> dict:
    path = _SPEC_FIXTURE if _SPEC_FIXTURE.exists() else _LOCAL_FIXTURE
    return instrument(json.loads(path.read_text()), name="crdt-tree/algebra.json")


def _scenario(name: str) -> dict:
    return next(item for item in _fixture()["scenarios"] if item["name"] == name)


def _replicas(scenario: dict) -> tuple[TextCrdt, dict[str, TextCrdt]]:
    base = TextCrdt.seed(scenario["seed"]["peer"], scenario["seed"]["text"])
    replicas: dict[str, TextCrdt] = {}
    for edit in scenario["replicas"]:
        replica = base.fork(edit["peer"])
        replica.insert(len(replica), edit["insert"])
        replicas[edit["name"]] = replica
    return base, replicas


def test_merge_algebra_is_order_and_duplication_independent() -> None:
    scenario = _scenario("merge algebra is order and duplication independent")
    base, replicas = _replicas(scenario)
    results: list[TextCrdt] = []
    for index, order in enumerate(scenario["merge_orders"]):
        merged = base.fork(100 + index)
        for name in order:
            merged.merge_from(replicas[name])
        results.append(merged)

    expect = scenario["expect"]
    assert isinstance(results[0], CrdtTree)
    assert_key(expect, "texts_equal", len({result.text() for result in results}) == 1)
    assert_key(
        expect,
        "version_vectors_equal",
        len({tuple(sorted(result.version_vector().items())) for result in results})
        == 1,
    )
    assert all(result.value() == result.text() for result in results)


def test_empty_frontier_snapshot_preserves_lineage() -> None:
    scenario = _scenario("empty frontier snapshot preserves lineage")
    source = TextCrdt.seed(scenario["seed"]["peer"], scenario["seed"]["text"])
    restored = TextCrdt(scenario["restore_peer"])

    expect = scenario["expect"]
    assert restored.apply_delta(source.delta_since({}))
    assert_key(expect, "restored_text_equal", restored.text() == source.text())
    source_ids = {(item.id.counter, item.id.peer) for item in source.elements()}
    restored_ids = {(item.id.counter, item.id.peer) for item in restored.elements()}
    # Lineage, not just text: a snapshot that re-minted op ids would round-trip
    # the same characters and still break every later merge.
    assert_key(expect, "op_ids_equal", restored_ids == source_ids)

    assert scenario["then_concurrent_edit"]
    source.insert(len(source), "a")
    restored.insert(len(restored), "b")
    source.merge_from(restored)
    restored.merge_from(source)
    assert source.text() == restored.text()
    ids = [(item.id.counter, item.id.peer) for item in source.elements()]
    assert_key(expect, "later_merge_duplicates", len(ids) - len(set(ids)))


def test_own_frontier_emits_empty_delta() -> None:
    scenario = _scenario("own frontier emits an empty delta")
    tree = TextCrdt.seed(scenario["seed"]["peer"], scenario["seed"]["text"])
    delta = tree.delta_since(tree.version_vector())
    expect = scenario["expect"]
    assert_key(expect, "delta", delta)
    assert_key(expect, "apply_changed", tree.apply_delta(delta))
