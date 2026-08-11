"""Cross-language conformance for the lossless tree CRDT (``#lzlosstree``).

Replays the canonical compute fixtures in
``lazily-spec/conformance/lossless-tree`` (the same nine files the Rust, JS,
and Kotlin bindings replay) through :class:`lazily.lossless_tree_crdt.LosslessTreeCrdt`.
Each fixture seeds an element/leaf tree, replays a step DSL (fork / sync /
deliver / on), and asserts ``render``, ``live_nodes``, and convergence.

A wire-schema compliance test exercises every M1 op variant through the
``lossless-tree-delta.json`` schema, and the ``TreeVersionFrontier`` shape
through ``lossless-tree.json`` — the same checks the Rust
``tests/lossless_tree_schema.rs`` makes. Both schemas are read through the
``LAZILY_SPEC_SCHEMAS_DIR`` seam (``#lzspecschemasoverride``).
"""

from __future__ import annotations

import itertools
import json

import pytest
from conformance_assert import (
    assert_key,
    assert_key_with,
    corpus_subdir,
    instrument,
    scenarios,
    schema_json,
    sub_entries,
)

from lazily.lossless_tree_crdt import (
    LEAF_KIND_FROM_WIRE,
    ROOT,
    LeafKind,
    LosslessTreeCrdt,
    SeedElement,
    SeedLeaf,
    TreeVersionFrontier,
    tree_update_to_wire,
    tree_version_frontier_to_wire,
)


_SPEC_FIXTURES = corpus_subdir("lossless-tree")


def _fixture(name: str) -> dict:
    path = _SPEC_FIXTURES / name
    assert path.exists(), f"missing spec fixture {name}"
    return instrument(json.loads(path.read_text()), name=f"lossless-tree/{name}")


def _seed(spec: dict) -> SeedElement | SeedLeaf:
    if "element" in spec:
        return SeedElement(kind=spec["element"])
    if "leaf" in spec:
        body = spec["leaf"]
        return SeedLeaf(kind=LEAF_KIND_FROM_WIRE[body["kind"]], text=body["text"])
    raise ValueError(f"node spec has neither element nor leaf: {spec!r}")


class _World:
    """A named world of replicas plus the shared label -> id map."""

    def __init__(self) -> None:
        self.replicas: dict[str, LosslessTreeCrdt] = {}
        self.ids: dict[str, object] = {}

    def id(self, label: str):
        if label not in self.ids:
            raise KeyError(f"unknown node label `{label}`")
        return self.ids[label]

    def after_of(self, op: dict):
        after = op.get("after")
        return None if after is None else self.id(after)

    def build_children(self, spec: dict, parent) -> None:
        children = spec.get("children")
        if not children:
            return
        prev = None
        for child in children:
            cid = self.replicas["a"].create_node(parent, prev, _seed(child))
            self.ids[child["label"]] = cid
            self.build_children(child, cid)
            prev = cid


def _apply_op(world: _World, on: str, op: dict) -> None:
    replica = world.replicas[on]
    kind = op["op"]
    if kind == "create":
        cid = replica.create_node(world.id(op["parent"]), world.after_of(op), _seed(op))
        world.ids[op["label"]] = cid
    elif kind == "edit_leaf":
        replica.edit_leaf(
            world.id(op["node"]),
            op["at_byte"],
            op.get("delete_bytes", 0),
            op.get("insert", ""),
        )
    elif kind == "split":
        world.ids[op["new_label"]] = replica.split_leaf(
            world.id(op["node"]), op["at_byte"]
        )
    elif kind == "merge_leaves":
        replica.merge_adjacent_leaves(world.id(op["left"]), world.id(op["right"]))
    elif kind == "reorder":
        replica.reorder_child(world.id(op["node"]), world.after_of(op))
    elif kind == "tombstone":
        replica.tombstone_node(world.id(op["node"]))
    else:
        raise ValueError(f"unknown op: {kind}")


def _deliver(world: _World, d: dict) -> None:
    """Partial anti-entropy: hand `to` a chosen slice of `from`'s canonical diff.

    The candidate list is the SAME `diff(to.frontier())` a full `sync` computes,
    sorted by the dotted `(counter, peer)` order `diff` already pins
    (#lzdifforderallbindings). Both selectors index into that list, 0-based:

      * `only`  — deliver that SUBSET, in canonical order. Which entries arrive,
                  not in what sequence.
      * `order` — deliver exactly those entries IN THE LISTED SEQUENCE, as ONE
                  `apply_update` call (#lzspecoutoforderfixtures). Re-sorting it,
                  or splitting it across calls, destroys the property the fixture
                  is testing: an op must arrive BEFORE the op it depends on, in
                  the same batch, so the dependency buffer is what recovers it.
                  A batch delivered one op per call would let plain re-delivery
                  paper over a missing buffer.

    Neither selector is clamped. An index past the end of the diff means the
    fixture and this runner disagree about what `from` is holding, and silently
    dropping it would deliver a SHORTER batch that still passes — so it raises.
    Exactly one selector must be present: defaulting a missing one to "all"
    would turn a typo'd key into a full sync that quietly converges.
    """
    src, dst = d["from"], d["to"]
    present = [key for key in ("only", "order") if key in d]
    if len(present) != 1:
        raise ValueError(
            f"deliver step must carry exactly one of `only` / `order`; got {present}"
        )
    selector = present[0]
    full = world.replicas[src].diff(world.replicas[dst].frontier())
    indexes = [int(i) for i in d[selector]]
    for i in indexes:
        if not 0 <= i < len(full.ops):
            raise IndexError(
                f"deliver {selector}={indexes} indexes op {i}, but the "
                f"{src}->{dst} diff holds only {len(full.ops)} op(s); an "
                f"out-of-range index is a disagreement about the diff, not "
                f"something to clamp"
            )
    if selector == "only":
        indexes = sorted(indexes)
    world.replicas[dst].apply_update(type(full)(ops=[full.ops[i] for i in indexes]))


def _apply_step(world: _World, step: dict) -> None:
    if "fork" in step:
        world.replicas[step["fork"]] = world.replicas["a"].fork(step["peer"])
    elif "sync" in step:
        src, dst = step["sync"]["from"], step["sync"]["to"]
        update = world.replicas[src].diff(world.replicas[dst].frontier())
        world.replicas[dst].apply_update(update)
    elif "deliver" in step:
        _deliver(world, step["deliver"])
    elif "on" in step:
        _apply_op(world, step["on"], step)
    else:
        raise ValueError(f"unrecognized step: {step}")


def _assert_expect(world: _World, expect: dict, label: str) -> None:
    if "render" in expect:
        assert_key(
            expect,
            "render",
            world.replicas["a"].render(),
            where=f"{label}: render on a",
        )
    if "render_on" in expect:
        # DESCEND (#lzsubblockkeyset): a child tracker owns the per-replica map,
        # so a replica the corpus adds fails as unconsumed instead of dropping
        # out of a dict comprehension keyed by the fixture's own names.
        for replica, want_render in sub_entries(
            expect, "render_on", where=f"{label}: render_on"
        ):
            assert world.replicas[replica].render() == want_render, (
                f"{label}: render on {replica}"
            )
    if "live_nodes" in expect:
        assert_key(
            expect,
            "live_nodes",
            world.replicas["a"].live_node_count(),
            where=f"{label}: live_nodes",
        )

    def _converged(names: list[str]) -> None:
        # The comparison is `names[1:]` against `names[0]`, so a ONE-element list
        # runs zero comparisons and a shortened list quietly drops a replica from
        # the claim — `["a","b"] -> ["a"]` was green (#lzconvergedlistlength).
        # The declared set must therefore cover every replica the scenario built:
        # convergence over a subset is not the claim the fixture is making.
        assert len(names) >= 2, (
            f"{label}: converged names {names} — a convergence claim over fewer "
            f"than two replicas compares nothing"
        )
        assert set(names) == set(world.replicas), (
            f"{label}: converged names {sorted(names)} do not cover every replica "
            f"the scenario built ({sorted(world.replicas)}); a subset can drop a "
            f"divergent replica from the claim without failing"
        )
        first = world.replicas[names[0]].render()
        for name in names[1:]:
            assert world.replicas[name].render() == first, (
                f"{label}: {names[0]}/{name} converge"
            )

    if "converged" in expect:
        assert_key_with(expect, "converged", _converged, where=label)


def _run_fixture(name: str) -> None:
    fixture = _fixture(name)
    for i, scenario in enumerate(scenarios(fixture)):
        label = f"{name}[{scenario.get('name', i)}]"
        world = _World()
        world.replicas["a"] = LosslessTreeCrdt(scenario["seed"]["peer"])
        world.build_children(scenario["seed"]["tree"], ROOT)
        for step in scenario.get("steps", []):
            _apply_step(world, step)
        _assert_expect(world, scenario["expect"], label)


_FIXTURE_NAMES = [
    "exact_roundtrip.json",
    "one_leaf_edit_delta.json",
    "split_merge.json",
    "concurrent_insert_same_parent.json",
    "concurrent_reorder_and_leaf_edit.json",
    "non_contiguous_anti_entropy.json",
    "token_trivia_preservation.json",
    "invalid_source_roundtrip.json",
    "concurrent_conflict_preserves_text.json",
    # The two apply_update rules no fork -> edit -> sync fixture could see
    # (lazily-spec 39df4b3, #lzspecoutoforderfixtures): the Lamport counter must
    # advance past every INGESTED op before the idempotence skip, and an op whose
    # dependency has not landed must be buffered and retried rather than dropped.
    "apply_update_advances_counter.json",
    "out_of_order_delivery_buffers.json",
]


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_lossless_tree_conformance(name: str) -> None:
    _run_fixture(name)


# ---------------------------------------------------------------------------
# `deliver` step contract (#lzspecoutoforderfixtures)
#
# These gate the RUNNER, not the library. `deliver.order` is the only way the
# corpus can state "this op arrived before the one it depends on", so a runner
# that clamps an out-of-range index, re-sorts the sequence, splits the batch, or
# defaults a missing selector to "everything" replays a DIFFERENT scenario and
# reports green for it.
# ---------------------------------------------------------------------------


def _two_op_world() -> _World:
    """`a` holds two ops `b` lacks: create `outer`, then create `inner` inside it."""
    world = _World()
    world.replicas["a"] = LosslessTreeCrdt(1)
    para = world.replicas["a"].create_node(ROOT, None, SeedElement("para"))
    world.ids["para"] = para
    world.replicas["b"] = world.replicas["a"].fork(2)
    outer = world.replicas["a"].create_node(para, None, SeedElement("wrap"))
    world.replicas["a"].create_node(outer, None, SeedLeaf(LeafKind.RAW, "deep"))
    return world


def _record_batches(monkeypatch: pytest.MonkeyPatch) -> list[list[object]]:
    """Record every `apply_update` batch. `LosslessTreeCrdt` uses `__slots__`, so
    the spy goes on the class, not on the instance."""
    batches: list[list[object]] = []
    real_apply = LosslessTreeCrdt.apply_update

    def _spy(self, update):
        batches.append(list(update.ops))
        real_apply(self, update)

    monkeypatch.setattr(LosslessTreeCrdt, "apply_update", _spy)
    return batches


def test_deliver_order_delivers_the_listed_sequence_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _two_op_world()
    canonical = world.replicas["a"].diff(world.replicas["b"].frontier()).ops
    assert len(canonical) == 2

    batches = _record_batches(monkeypatch)
    _deliver(world, {"from": "a", "to": "b", "order": [1, 0]})

    # ONE call, carrying exactly the listed indexes in the listed sequence — the
    # child's create ahead of its parent's. Re-sorting or splitting this would
    # hand the library an in-order batch and hide a missing dependency buffer.
    assert len(batches) == 1
    assert batches[0] == [canonical[1], canonical[0]]
    assert world.replicas["b"].render() == "deep"


def test_deliver_only_delivers_its_subset_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _two_op_world()
    canonical = world.replicas["a"].diff(world.replicas["b"].frontier()).ops

    batches = _record_batches(monkeypatch)
    _deliver(world, {"from": "a", "to": "b", "only": [1, 0]})

    assert batches == [[canonical[0], canonical[1]]]


def test_deliver_order_rejects_an_out_of_range_index_instead_of_clamping() -> None:
    world = _two_op_world()
    with pytest.raises(IndexError, match="out-of-range"):
        _deliver(world, {"from": "a", "to": "b", "order": [2, 1, 0]})
    # Nothing was delivered. A clamping runner would have shipped a SHORTER batch
    # and still converged, so the fixture would pass while replaying two ops
    # instead of three.
    assert world.replicas["b"].render() == ""


def test_deliver_only_rejects_an_out_of_range_index_instead_of_clamping() -> None:
    world = _two_op_world()
    with pytest.raises(IndexError, match="out-of-range"):
        _deliver(world, {"from": "a", "to": "b", "only": [0, 5]})
    assert world.replicas["b"].render() == ""


@pytest.mark.parametrize(
    "selectors",
    [
        pytest.param({"only": [0], "order": [0]}, id="both"),
        pytest.param({}, id="neither"),
    ],
)
def test_deliver_requires_exactly_one_selector(selectors: dict) -> None:
    world = _two_op_world()
    with pytest.raises(ValueError, match="exactly one of"):
        _deliver(world, {"from": "a", "to": "b", **selectors})
    assert world.replicas["b"].render() == ""


# ---------------------------------------------------------------------------
# Unit properties mirroring the Rust inline tests
# ---------------------------------------------------------------------------


def test_render_is_exact_concatenation_including_multibyte() -> None:
    t = LosslessTreeCrdt(1)
    heading = t.create_node(ROOT, None, SeedElement("heading"))
    prev = t.create_node(heading, None, SeedLeaf(LeafKind.TOKEN, "# "))
    prev = t.create_node(heading, prev, SeedLeaf(LeafKind.RAW, "héllo"))
    t.create_node(heading, prev, SeedLeaf(LeafKind.TRIVIA, "\n"))
    assert t.render() == "# héllo\n"
    assert t.live_node_count() == 4


def test_edit_leaf_at_byte_offset_into_multibyte_text() -> None:
    t = LosslessTreeCrdt(1)
    h = t.create_node(ROOT, None, SeedElement("h"))
    leaf = t.create_node(h, None, SeedLeaf(LeafKind.RAW, "héllo"))
    t.edit_leaf(leaf, 3, 0, "X")  # byte 3 lands after the 2-byte é
    assert t.render() == "héXllo"


def test_edit_leaf_rejects_non_char_boundary() -> None:
    from lazily.lossless_tree_crdt import TreeError

    t = LosslessTreeCrdt(1)
    leaf = t.create_node(ROOT, None, SeedLeaf(LeafKind.RAW, "héllo"))
    with pytest.raises(TreeError):
        t.edit_leaf(leaf, 2, 0, "X")  # byte 2 is inside the 2-byte é


def test_diff_apply_converges_two_replicas() -> None:
    t = LosslessTreeCrdt(1)
    para = t.create_node(ROOT, None, SeedElement("para"))
    a = t.create_node(para, None, SeedLeaf(LeafKind.RAW, "hello"))
    other = t.fork(2)
    other.edit_leaf(a, 5, 0, "!")
    other.create_node(para, a, SeedLeaf(LeafKind.TOKEN, "."))
    t.apply_update(other.diff(t.frontier()))
    other.apply_update(t.diff(other.frontier()))
    assert t.render() == other.render()


def test_non_contiguous_delivery_leaves_a_recoverable_hole() -> None:
    t = LosslessTreeCrdt(1)
    para = t.create_node(ROOT, None, SeedElement("para"))
    base = t.create_node(para, None, SeedLeaf(LeafKind.TRIVIA, "0"))
    # Fork BEFORE emitting the sibling ops so b lacks them.
    b = t.fork(2)
    t.create_node(para, base, SeedLeaf(LeafKind.TRIVIA, "1"))
    t.create_node(para, base, SeedLeaf(LeafKind.TRIVIA, "2"))
    t.create_node(para, base, SeedLeaf(LeafKind.TRIVIA, "3"))
    full = t.diff(b.frontier())
    # deliver ops at indices 0 and 2 only (hole at 1)
    b.apply_update(type(full)(ops=[full.ops[0], full.ops[2]]))
    assert t.render() != b.render()
    # one follow-up diff re-requests exactly the missing op
    repair = t.diff(b.frontier())
    assert len(repair.ops) == 1
    b.apply_update(repair)
    assert t.render() == b.render()
    assert t.frontier() == b.frontier()


def test_diff_returns_ops_in_canonical_counter_peer_order() -> None:
    """Diff order is a cross-binding contract the corpus cannot pin itself.

    ``lossless-tree/non_contiguous_anti_entropy.json`` addresses ops
    POSITIONALLY (``deliver.only: [0, 2]``), so the fixture only means the same
    thing in every binding while every binding returns the same order. The
    corpus cannot catch a broken sort: measured in lazily-zig
    (commit e8a2a28), replacing the sort with a reverse — or deleting it —
    left the entire suite green, because either way the two indices select the
    same SET and ``apply_update`` is order-tolerant by design. Only a direct
    assertion pins it (``#lzdifforderallbindings``).
    """
    a = LosslessTreeCrdt(1)
    para = a.create_node(ROOT, None, SeedElement("para"))
    base = a.create_node(para, None, SeedLeaf(LeafKind.TRIVIA, "0"))

    b = a.fork(2)

    # a runs ahead to counter 4 while b's single op stays at counter 3, so b's
    # op arrives LAST in a's log yet sorts EARLIER than a's own (4, 1). Without
    # that gap the log would already be canonical and the ordering assertion
    # below would hold for a reversed diff too — pinning nothing.
    one = a.create_node(para, base, SeedLeaf(LeafKind.TRIVIA, "1"))
    a.create_node(para, one, SeedLeaf(LeafKind.TRIVIA, "2"))
    b.create_node(para, base, SeedLeaf(LeafKind.TRIVIA, "9"))
    a.apply_update(b.diff(a.frontier()))

    ops = a.diff(TreeVersionFrontier()).ops
    logged = [op.id for op in a._log]

    # Non-vacuity, asserted EXPLICITLY: log order and canonical order genuinely
    # differ here. If a future refactor makes them coincide this fails loudly
    # instead of letting the ordering check below go quietly vacuous.
    assert len(ops) == len(logged)
    assert [op.id for op in ops] != logged

    keys = [(op.id.counter, op.id.peer) for op in ops]
    assert all(prev < curr for prev, curr in itertools.pairwise(keys)), keys


# ---------------------------------------------------------------------------
# Wire schema compliance
# ---------------------------------------------------------------------------

jsonschema = pytest.importorskip("jsonschema")
referencing = pytest.importorskip("referencing")
from referencing import Registry  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402


def _registry() -> Registry:
    names = ["lossless-tree", "lossless-tree-delta"]
    schemas = {
        f"https://lazily.dev/schemas/{n}.json": schema_json(f"{n}.json") for n in names
    }
    resources = [
        (uri, DRAFT202012.create_resource(schema)) for uri, schema in schemas.items()
    ]
    return Registry().with_resources(resources)


def test_emitted_tree_update_validates_delta_schema() -> None:
    validator = jsonschema.Draft202012Validator(
        schema_json("lossless-tree-delta.json"),
        registry=_registry(),
    )
    t = LosslessTreeCrdt(1)
    para = t.create_node(ROOT, None, SeedElement("para"))
    a = t.create_node(para, None, SeedLeaf(LeafKind.RAW, "hello world"))
    b = t.create_node(para, a, SeedLeaf(LeafKind.TOKEN, "!"))
    t.edit_leaf(a, 5, 0, "X")  # LeafEdit
    tail = t.split_leaf(a, 6)  # SplitLeaf
    t.merge_adjacent_leaves(a, tail)  # MergeLeaves
    t.reorder_child(b, None)  # Reorder
    t.tombstone_node(b)  # Tombstone
    wire = tree_update_to_wire(t.diff(TreeVersionFrontier()))
    validator.validate(wire)


def test_frontier_validates_vocabulary_schema() -> None:
    vocab = schema_json("lossless-tree.json")
    frontier_def = {"$ref": "#/$defs/TreeVersionFrontier"}
    validator = jsonschema.Draft202012Validator(
        {"$defs": vocab["$defs"], **frontier_def}
    )
    t = LosslessTreeCrdt(1)
    t.create_node(ROOT, None, SeedLeaf(LeafKind.RAW, "ab"))
    t.create_node(ROOT, None, SeedLeaf(LeafKind.RAW, "cd"))
    # Punch a non-contiguous hole so sparse is exercised.
    wire = tree_version_frontier_to_wire(t.frontier())
    assert "sparse" in wire["dots"]["1"]
    validator.validate(wire)


def test_delta_schema_rejects_base64_frac_and_lowercase_leaf_kind() -> None:
    validator = jsonschema.Draft202012Validator(
        schema_json("lossless-tree-delta.json"),
        registry=_registry(),
    )
    good = {
        "ops": [
            {
                "id": {"counter": 1, "peer": 1},
                "kind": {
                    "CreateNode": {
                        "id": {"counter": 1, "peer": 1},
                        "parent": {"counter": 0, "peer": 0},
                        "sort": {"frac": [128], "peer": 1},
                        "seed": {"Leaf": {"kind": "Raw", "text": "x"}},
                    }
                },
            }
        ]
    }
    validator.validate(good)

    base64_frac = json.loads(json.dumps(good))
    base64_frac["ops"][0]["kind"]["CreateNode"]["sort"]["frac"] = "AAAA"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(base64_frac)

    lowercase = json.loads(json.dumps(good))
    lowercase["ops"][0]["kind"]["CreateNode"]["seed"]["Leaf"]["kind"] = "raw"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(lowercase)
