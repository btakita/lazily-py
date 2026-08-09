"""Cross-language conformance for the lossless tree CRDT (``#lzlosstree``).

Replays the canonical compute fixtures in
``lazily-spec/conformance/lossless-tree`` (the same nine files the Rust, JS,
and Kotlin bindings replay) through :class:`lazily.lossless_tree_crdt.LosslessTreeCrdt`.
Each fixture seeds an element/leaf tree, replays a step DSL (fork / sync /
deliver / on), and asserts ``render``, ``live_nodes``, and convergence.

A wire-schema compliance test exercises every M1 op variant through the
``lossless-tree-delta.json`` schema, and the ``TreeVersionFrontier`` shape
through ``lossless-tree.json`` — the same checks the Rust
``tests/lossless_tree_schema.rs`` makes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance_assert import (
    assert_key,
    assert_key_with,
    corpus_subdir,
    instrument,
    scenarios,
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


def _apply_step(world: _World, step: dict) -> None:
    if "fork" in step:
        world.replicas[step["fork"]] = world.replicas["a"].fork(step["peer"])
    elif "sync" in step:
        src, dst = step["sync"]["from"], step["sync"]["to"]
        update = world.replicas[src].diff(world.replicas[dst].frontier())
        world.replicas[dst].apply_update(update)
    elif "deliver" in step:
        d = step["deliver"]
        src, dst, only = d["from"], d["to"], d["only"]
        full = world.replicas[src].diff(world.replicas[dst].frontier())
        world.replicas[dst].apply_update(type(full)(ops=[full.ops[i] for i in only]))
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
]


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_lossless_tree_conformance(name: str) -> None:
    _run_fixture(name)


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


# ---------------------------------------------------------------------------
# Wire schema compliance
# ---------------------------------------------------------------------------

jsonschema = pytest.importorskip("jsonschema")
referencing = pytest.importorskip("referencing")
from referencing import Registry  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402


_SPEC_SCHEMAS = Path(__file__).resolve().parents[2] / "lazily-spec" / "schemas"


def _registry() -> Registry:
    names = ["lossless-tree", "lossless-tree-delta"]
    schemas = {
        f"https://lazily.dev/schemas/{n}.json": json.loads(
            (_SPEC_SCHEMAS / f"{n}.json").read_text()
        )
        for n in names
    }
    resources = [
        (uri, DRAFT202012.create_resource(schema)) for uri, schema in schemas.items()
    ]
    return Registry().with_resources(resources)


def test_emitted_tree_update_validates_delta_schema() -> None:
    validator = jsonschema.Draft202012Validator(
        json.loads((_SPEC_SCHEMAS / "lossless-tree-delta.json").read_text()),
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
    vocab = json.loads((_SPEC_SCHEMAS / "lossless-tree.json").read_text())
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
        json.loads((_SPEC_SCHEMAS / "lossless-tree-delta.json").read_text()),
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
