"""Conformance tests for the collection-layer compute fixtures.

Each test loads a canonical JSON fixture from ``lazily-spec/conformance/collections``
and replays it through the matching lazily-py model, asserting the spec's
language-agnostic expectations. The fixtures are the same files the Rust and Zig
bindings test against, so all implementations stay byte-compatible on the
compute invariants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conformance_assert import (
    assert_key,
    assert_key_with,
    corpus_fixture,
    instrument,
    scenarios,
    sub_entries,
)

from lazily import (
    CrdtOp,
    CrdtPlaneRuntime,
    NodeKey,
    SemTree,
    SeqCrdt,
    TextCrdt,
    WireStamp,
    align,
    assign_stable_keys,
    block_key,
    similarity,
)
from lazily.ipc import IpcValue_Inline


_LOCAL = Path(__file__).resolve().parent / "conformance"


def _load(rel: str) -> dict:
    path = corpus_fixture(rel, _LOCAL / rel)
    return instrument(json.loads(path.read_text()), name=rel)


# ---------------------------------------------------------------------------
# Stable-id alignment (manufactured identity)
# ---------------------------------------------------------------------------


def test_stableid_alignment_conformance() -> None:
    fix = _load("collections/stableid_alignment.json")
    assert fix["model"] == "StableId"
    for sc in scenarios(fix):
        name = sc["name"]
        expect = sc["expect"]
        if "blocks" in sc:
            # key-equality scenarios
            keys = [block_key(b) for b in sc["blocks"]]
            if "key_equal" in expect:
                assert_key_with(
                    expect,
                    "key_equal",
                    lambda want, keys=keys: all(keys[i] == keys[j] for i, j in want),
                    where=f"{name}: key_equal",
                )
            if "key_not_equal" in expect:
                assert_key_with(
                    expect,
                    "key_not_equal",
                    lambda want, keys=keys: all(keys[i] != keys[j] for i, j in want),
                    where=f"{name}: key_not_equal",
                )
            continue
        if "new_key_equals_old_key" in expect:
            keys = assign_stable_keys(sc["old"], sc["new"])
            assert_key_with(
                expect,
                "new_key_equals_old_key",
                lambda want, keys=keys, old=sc["old"]: all(
                    keys[ni] == block_key(old[oi]) for ni, oi in want
                ),
                where=f"{name}: new key equals old key",
            )
            continue
        if "old" in sc and "new" in sc:
            a = align(sc["old"], sc["new"])
            assert_key(expect, "matches", a.matches, where=name)
            assert_key(expect, "removed", a.removed, where=name)
            if "similarity_min" in expect:
                sim = similarity(sc["old"][0]["text"], sc["new"][0]["text"])
                assert_key_with(
                    expect,
                    "similarity_min",
                    lambda lo, sim=sim: sim >= lo,
                    where=f"{name}: similarity {sim}",
                )


# ---------------------------------------------------------------------------
# Memoized semantic tree
# ---------------------------------------------------------------------------


def test_semtree_conformance() -> None:
    fix = _load("collections/semtree_incremental.json")
    assert fix["model"] == "SemTree"
    for sc in scenarios(fix):
        tree: SemTree[str, int] = SemTree.from_json(sc["tree"], fold=sc["fold"])  # type: ignore[arg-type]
        expect_initial = sc["expect_initial"]
        for node_id in list(expect_initial):
            assert_key(
                expect_initial,
                node_id,
                tree.derived(node_id),
                where=f"{sc['name']}: initial derived({node_id})",
            )
        # Reset the recomputation counters after the warm-up so the edit-phase
        # counts measure ONLY the edit's effect.
        for node in tree._nodes.values():
            node.compute_count = 0
            node.downstream_count = 0
        if "edit" in sc:
            tree.set_node_value(sc["edit"]["id"], sc["edit"]["value"])
            expect_after = sc["expect_after"]
            for node_id in list(expect_after):
                # These two are booleans ABOUT the recomputation, not node
                # derivations, and both used to be skipped past after the loop
                # had already marked them read (#lzconsumednotasserted). They are
                # real observable facts, so they are asserted against the
                # fixture's own value rather than a hardcoded 0.
                if node_id == "sibling_a_cached":
                    # An edit to b1 must not recompute the sibling subtree 'a'.
                    assert_key(
                        expect_after,
                        node_id,
                        tree.node("a").compute_count == 0,
                        where=f"{sc['name']}: sibling 'a' cached",
                    )
                    continue
                if node_id == "downstream_consumer_reran":
                    assert_key(
                        expect_after,
                        node_id,
                        tree.node("root").downstream_count > 0,
                        where=f"{sc['name']}: downstream consumer re-ran",
                    )
                    continue
                assert_key(
                    expect_after,
                    node_id,
                    tree.derived(node_id),
                    where=f"{sc['name']}: after edit derived({node_id})",
                )
        if "remove_child" in sc:
            tree.remove_child(sc["remove_child"]["parent"], sc["remove_child"]["child"])
            expect_after = sc["expect_after"]
            for node_id in list(expect_after):
                assert_key(
                    expect_after,
                    node_id,
                    tree.derived(node_id),
                    where=f"{sc['name']}: after remove derived({node_id})",
                )


# ---------------------------------------------------------------------------
# TextCrdt convergence
# ---------------------------------------------------------------------------


class _Replicas:
    """A tiny step interpreter for the textcrdt fixtures."""

    def __init__(self) -> None:
        self.r: dict[str, TextCrdt] = {}

    def seed(self, spec: Any) -> TextCrdt:
        if isinstance(spec, str):
            return TextCrdt.seed(1, spec)
        return TextCrdt.seed(spec["peer"], spec["text"])

    def run(self, steps: list[dict], default_seed: Any = None) -> None:
        for st in steps:
            if "fork" in st:
                src = self.r.get("a")
                if src is not None:
                    self.r[st["fork"]] = src.clone()
                    self.r[st["fork"]].peer = st["peer"]
                else:
                    self.r[st["fork"]] = TextCrdt(st["peer"])
                continue
            if "clone" in st:
                self.r[st["clone"]] = self.r[st["from"]].clone()
                continue
            if "merge" in st:
                self.r[st["merge"]["into"]].merge(self.r[st["merge"]["from"]])
                continue
            if st.get("op") == "gc":
                target = self.r.get("a")
                if target is None:
                    continue
                collected = target.gc(stable=st["stable"])
                if "expect_collected" in st:
                    assert collected == st["expect_collected"], (
                        f"gc collected {collected}, want {st['expect_collected']}"
                    )
                continue
            if "on" in st:
                target = self.r[st["on"]]
                self._op(target, st)
                continue
            if "op" in st:
                # default replica 'a'
                target = self.r.get("a")
                if target is None:
                    continue
                self._op(target, st)
                continue
            # A step shape this interpreter does not recognize used to fall off
            # the end of the chain and be skipped in silence, while the scenario
            # ledger still booked the scenario as replayed — the fixture named
            # behaviour this runner never performed and reported green
            # (#lzscenariobodyskip).
            raise AssertionError(f"unrecognized textcrdt step shape {sorted(st)!r}")

    def _op(self, target: TextCrdt, st: dict) -> None:
        op = st["op"]
        if op == "insert":
            target.insert(st["index"], st["ch"])
        elif op == "insert_str":
            target.insert_str(st["index"], st["str"])
        elif op == "delete":
            target.delete(st["index"])
        else:
            # No final `else` here meant an op name this interpreter does not
            # implement silently did nothing, and the convergence assertions
            # still ran against the un-mutated replica (#lzscenariobodyskip).
            raise AssertionError(f"unknown textcrdt op {op!r}")


def test_textcrdt_convergence_conformance() -> None:
    fix = _load("collections/textcrdt_convergence.json")
    assert fix["model"] == "TextCrdt"
    for sc in scenarios(fix):
        interp = _Replicas()
        seed = sc.get("seed") or sc.get("replica")
        if seed is not None:
            if isinstance(seed, dict):
                interp.r["a"] = interp.seed(seed)
            else:
                # seed is a bare string; replica.peer carries the peer
                peer = sc.get("replica", {}).get("peer", 1)
                interp.r["a"] = TextCrdt.seed(peer, seed)
        interp.run(sc["steps"])
        exp = sc["expect"]
        name = sc["name"]
        if "text" in exp:
            assert_key(exp, "text", interp.r["a"].text(), where=name)
        if "len" in exp:
            assert_key(exp, "len", len(interp.r["a"]), where=name)
        if "tombstone_count" in exp:
            assert_key(
                exp, "tombstone_count", interp.r["a"].tombstone_count(), where=name
            )
        if "texts_equal" in exp:
            assert_key_with(
                exp,
                "texts_equal",
                lambda want, interp=interp: all(
                    interp.r[left].text() == interp.r[right].text()
                    for left, right in want
                ),
                where=name,
            )
        if "a_starts_with" in exp:
            assert_key_with(
                exp,
                "a_starts_with",
                lambda want, interp=interp: interp.r["a"].text().startswith(want),
                where=name,
            )
        if "a_ends_with" in exp:
            assert_key_with(
                exp,
                "a_ends_with",
                lambda want, interp=interp: interp.r["a"].text().endswith(want),
                where=name,
            )


def test_textcrdt_delta_sync_conformance() -> None:
    fix = _load("collections/textcrdt_delta_sync.json")
    assert fix["model"] == "TextCrdt"
    for sc in scenarios(fix):
        interp = _Replicas()
        seed = sc["seed"]
        interp.r["a"] = TextCrdt.seed(seed["peer"], seed["text"])
        # Seed peer name 'a' is the default.
        for st in sc["steps"]:
            if "fork" in st:
                src = interp.r.get("a") or interp.r.get("a1")
                interp.r[st["fork"]] = src.clone()
                interp.r[st["fork"]].peer = st["peer"]
                continue
            if "new" in st:
                interp.r[st["new"]] = TextCrdt(st["peer"])
                continue
            if "snapshot" in st:
                snap = interp.r[st["snapshot"]["from"]].delta_since({})
                interp.r[st["snapshot"]["into"]] = TextCrdt(st["snapshot"]["peer"])
                changed = interp.r[st["snapshot"]["into"]].apply_delta(snap)
                if "expect_changed" in st["snapshot"]:
                    assert changed == st["snapshot"]["expect_changed"], sc["name"]
                continue
            if "delta" in st:
                delta = interp.r[st["delta"]["from"]].delta_since(
                    interp.r[st["delta"]["into"]].version_vector()
                )
                changed = interp.r[st["delta"]["into"]].apply_delta(delta)
                if "expect_changed" in st["delta"]:
                    assert changed == st["delta"]["expect_changed"], sc["name"]
                continue
            if "exchange" in st:
                # bidirectional delta exchange
                left, right = st["exchange"]
                d_lr = interp.r[left].delta_since(interp.r[right].version_vector())
                d_rl = interp.r[right].delta_since(interp.r[left].version_vector())
                interp.r[left].apply_delta(d_rl)
                interp.r[right].apply_delta(d_lr)
                continue
            if "on" in st:
                interp._op(interp.r[st["on"]], st)
                continue
            # Fail closed: an unrecognized step shape used to be skipped in
            # silence while the ledger booked the scenario as replayed
            # (#lzscenariobodyskip).
            raise AssertionError(
                f"unrecognized textcrdt delta-sync step shape {sorted(st)!r}"
            )
        exp = sc["expect"]
        name = sc["name"]
        if "texts_equal" in exp:
            assert_key_with(
                exp,
                "texts_equal",
                lambda want, interp=interp: all(
                    interp.r[left].text() == interp.r[right].text()
                    for left, right in want
                ),
                where=name,
            )
        # DESCEND into the per-replica maps (#lzsubblockkeyset): each is an
        # object whose key set is the set of replicas the claim covers, and a
        # replica the corpus adds must fail rather than drop out of a
        # comprehension driven by the fixture's own names.
        if "text_on" in exp:
            for who, want_text in sub_entries(exp, "text_on", where=f"{name}: text_on"):
                assert interp.r[who].text() == want_text, f"{name}: text on {who}"
        if "version_vector_on" in exp:
            vectors = exp.sub("version_vector_on")
            for who in sorted(vectors):
                got = interp.r[who].version_vector()
                # Whole-object equality per replica, with the OBSERVED side keyed
                # the way the corpus spells it, so the vector's own peer set is
                # compared too.
                assert_key(
                    vectors,
                    who,
                    {str(peer): count for peer, count in got.items()},
                    where=f"{name}: version_vector_on[{who}]",
                )


# ---------------------------------------------------------------------------
# SeqCrdt convergence
# ---------------------------------------------------------------------------


class _SeqReplicas:
    def __init__(self) -> None:
        self.r: dict[str, SeqCrdt] = {}

    def run(self, steps: list[dict]) -> None:
        for st in steps:
            if "fork" in st:
                # `SeqCrdt.fork` rather than clone-then-reassign-peer: a fork
                # must carry the source's causal clock, and this step is the
                # corpus's only fork (#lzzigforkhlcpeer). The two are equivalent
                # here — the clock holds no peer — but naming the operation
                # keeps the replay honest about what the fixture step means.
                src = self.r.get("a")
                self.r[st["fork"]] = (
                    src.fork(st["peer"]) if src is not None else SeqCrdt(st["peer"])
                )
                continue
            if "clone" in st:
                self.r[st["clone"]] = self.r[st["from"]].clone()
                continue
            if "merge" in st:
                self.r[st["merge"]["into"]].merge(
                    self.r[st["merge"]["from"]], now=st.get("now", 0)
                )
                continue
            if "on" in st:
                self._op(self.r[st["on"]], st)
                continue
            if "op" in st:
                target = self.r.get("a")
                if target is not None:
                    self._op(target, st)
                continue
            # Fail closed on a step shape this interpreter does not recognize:
            # falling off the chain skipped the step in silence while the
            # scenario ledger booked it as replayed (#lzscenariobodyskip).
            raise AssertionError(f"unrecognized seqcrdt step shape {sorted(st)!r}")

    def _op(self, target: SeqCrdt, st: dict) -> None:
        op = st["op"]
        if op == "insert_back":
            target.insert_back(st["id"], st["value"], st["now"])
        elif op == "insert_front":
            target.insert_front(st["id"], st["value"], st["now"])
        elif op == "move_after":
            target.move_after(st["id"], st["anchor"], st["now"])
        elif op == "set_value":
            target.set_value(st["id"], st["value"], st["now"])
        elif op == "remove":
            target.remove(st["id"], st["now"])
        else:
            # Without this arm an op name the interpreter does not implement
            # silently did nothing and the convergence expectations were
            # asserted against an un-mutated replica (#lzscenariobodyskip).
            raise AssertionError(f"unknown seqcrdt op {op!r}")


def test_seqcrdt_convergence_conformance() -> None:
    fix = _load("collections/seqcrdt_convergence.json")
    assert fix["model"] == "SeqCrdt"
    for sc in scenarios(fix):
        interp = _SeqReplicas()
        if "replica" in sc:
            interp.r["a"] = SeqCrdt(sc["replica"]["peer"])
        if "seed" in sc:
            interp.r["a"] = SeqCrdt.seed(sc["seed"]["peer"], sc["seed"]["inserts"])
        interp.run(sc["steps"])
        exp = sc["expect"]
        # Resolve which replicas the global checks (`len`, `contains_all`) apply
        # to: when the scenario converges via merge, the merged result; else 'a'.
        merged_replicas: list[str] = []
        for pair in exp.get("orders_equal", []):
            merged_replicas.extend(pair)
        for who in exp.get("order_on", {}):
            merged_replicas.append(who)
        len_targets = merged_replicas if merged_replicas else ["a"]
        name = sc["name"]
        if "order" in exp:
            assert_key(exp, "order", interp.r["a"].order(), where=name)
        if "len" in exp:
            assert_key_with(
                exp,
                "len",
                lambda want, interp=interp, targets=len_targets: all(
                    len(interp.r[who]) == want for who in targets
                ),
                where=f"{name}: len over {len_targets}",
            )
        if "get" in exp:
            # The observed side is built by probing EVERY key the fixture names,
            # so whole-object equality compares the object's key set as well as
            # its values (#lzsubblockkeyset).
            assert_key(
                exp,
                "get",
                {k: interp.r["a"].get(k) for k in exp["get"]},
                where=name,
            )
        if "order_on" in exp:
            for who, want_order in sub_entries(
                exp, "order_on", where=f"{name}: order_on"
            ):
                assert interp.r[who].order() == want_order, f"{name}: order on {who}"
        if "get_on" in exp:
            per_replica = exp.sub("get_on")
            for who in sorted(per_replica):
                assert_key(
                    per_replica,
                    who,
                    {k: interp.r[who].get(k) for k in per_replica[who]},
                    where=f"{name}: get_on[{who}]",
                )
        if "orders_equal" in exp:
            assert_key_with(
                exp,
                "orders_equal",
                lambda want, interp=interp: all(
                    interp.r[left].order() == interp.r[right].order()
                    for left, right in want
                ),
                where=name,
            )
        if "not_contains_on" in exp:
            for who, items in sub_entries(
                exp, "not_contains_on", where=f"{name}: not_contains_on"
            ):
                for item in items:
                    assert item not in interp.r[who], (
                        f"{name}: {item!r} must be absent from {who}"
                    )
        if "contains_all" in exp:
            assert_key_with(
                exp,
                "contains_all",
                lambda want, interp=interp, targets=len_targets: all(
                    item in interp.r[who] for item in want for who in targets
                ),
                where=f"{name}: contains_all",
            )


# ---------------------------------------------------------------------------
# CRDT plane anti-entropy
# ---------------------------------------------------------------------------


def _mk_crdtop(d: dict) -> CrdtOp:
    s = d["stamp"]
    return CrdtOp(
        d["node"],
        NodeKey.new(d["key"]) if d.get("key") else None,
        WireStamp(s["wall_time"], s["logical"], s["peer"]),
        IpcValue_Inline(bytes(d["state"]["Inline"])),
    )


def test_crdt_plane_anti_entropy_conformance() -> None:
    fix = _load("distributed/anti_entropy_converge.json")
    assert fix["model"] == "CrdtPlane"
    for sc in scenarios(fix):
        expect = sc["expect"]
        name = sc["name"]
        plane = CrdtPlaneRuntime()
        applied = plane.apply_ops([_mk_crdtop(o) for o in sc["ops"]])
        assert_key(expect, "applied_count", applied, where=name)

        # `resolution`: the winner is the op with the greatest WireStamp, not
        # the last one delivered. Asserting the converged state alone cannot
        # tell those apart when they coincide, so pin the rule itself — and let
        # the fixture's spelling select the rule to enforce, fail-closed, rather
        # than checking it against a literal that asserts nothing about the plane.
        by_node: dict[int, dict] = {}
        for op in sc["ops"]:
            stamp = op["stamp"]
            key = (stamp["wall_time"], stamp["logical"], stamp["peer"])
            best = by_node.get(op["node"])
            if best is None or key > best["_key"]:
                by_node[op["node"]] = {**op, "_key": key}

        def _resolution(
            want: str, name: str = name, plane: Any = plane, by_node: dict = by_node
        ) -> None:
            assert want == "max_stamp", (
                f"{name}: unimplemented resolution rule {want!r}"
            )
            for entry in plane.converged():
                winner = by_node[entry.node]
                assert entry.state == bytes(winner["state"]["Inline"]), (
                    f"{name}: node {entry.node} did not resolve to the max stamp"
                )

        assert_key_with(expect, "resolution", _resolution, where=name)

        if sc.get("redeliver"):
            rd = plane.apply_ops([_mk_crdtop(o) for o in sc["ops"]])
            assert_key(expect, "redeliver_applied_count", rd, where=name)
        if sc.get("reverse_order_equivalent"):
            plane2 = CrdtPlaneRuntime()
            plane2.apply_ops([_mk_crdtop(o) for o in reversed(sc["ops"])])
            got = {(e.node, e.state) for e in plane.converged()}
            got2 = {(e.node, e.state) for e in plane2.converged()}
            assert_key(expect, "order_independent", got == got2, where=name)

        def _converged(wants: list[dict], name: str = name, plane: Any = plane) -> None:
            # Per-element membership only ever adds obligations, never counts
            # them, so DELETING an entry silently removed one and stayed green
            # (#lzconvergedlistlength). The declared list must account for every
            # converged entry the plane produced, or the corpus can shrink its
            # own expectation.
            entries = plane.converged()
            assert len(wants) == len(entries), (
                f"{name}: expect.converged declares {len(wants)} entries but the "
                f"plane converged {len(entries)}; a shorter list drops an "
                f"obligation without failing"
            )
            for want in wants:
                matches = [
                    e
                    for e in plane.converged()
                    if e.node == want["node"]
                    and (
                        (e.key is None and not want.get("key"))
                        or (e.key is not None and e.key.path == want.get("key"))
                    )
                ]
                assert matches, f"{name}: no converged entry for node {want['node']}"
                assert matches[0].state == bytes(want["state"]["Inline"]), (
                    f"{name}: converged state mismatch for node {want['node']}"
                )

        assert_key_with(expect, "converged", _converged, where=name)


def _canonicalize_crdt_sync_wire(wire: dict[str, Any]) -> dict[str, Any]:
    """Fill in the declared default for an omitted ``CrdtSync.frontier``.

    ``#lzspecfrontiersuppress``: omitted and ``[]`` are declared equivalent by
    ``schemas/distributed.json``, so the round-trip comparison is semantic.
    """
    inner = wire.get("CrdtSync")
    if not isinstance(inner, dict) or "frontier" in inner:
        return wire
    return {"CrdtSync": {"frontier": [], **inner}}


def test_crdt_sync_frames_round_trip() -> None:
    fix = _load("distributed/crdt_sync_frames.json")
    assert fix["kind"] == "CrdtSyncFrames"
    from lazily import IpcMessage

    for frame in fix["frames"]:
        wire = frame["wire"]
        msg = IpcMessage.from_wire(wire)
        assert msg.is_crdt_sync
        # Round-trip is byte-for-byte except for schema-declared-equivalent
        # encodings (lazily-spec docs/conformance.md § Round-trip equivalence
        # exemptions): `CrdtSync.frontier` omitted is equivalent to `[]`.
        assert msg.to_wire() == _canonicalize_crdt_sync_wire(wire), (
            f"round-trip mismatch: {frame['label']}"
        )
        a = frame["assertions"]
        sync = msg.crdt_sync
        label = frame["label"]
        if "frontier_len" in a:
            assert_key(a, "frontier_len", len(sync.frontier), where=label)
        if "frontier_omitted" in a:
            # #lzspecfrontiersuppress: an omitted frontier decodes as empty.
            assert_key(
                a, "frontier_omitted", "frontier" not in wire["CrdtSync"], where=label
            )
            assert sync.frontier == [], label
        assert_key(a, "op_count", len(sync.ops), where=label)
        # Both directions: these used to be membership gates, so a fixture
        # declaring `false` still asserted the op WAS there
        # (#lzconsumednotasserted).
        if "has_keyed_op" in a:
            assert_key(
                a,
                "has_keyed_op",
                any(op.key is not None for op in sync.ops),
                where=label,
            )
        if "has_keyless_op" in a:
            assert_key(
                a, "has_keyless_op", any(op.key is None for op in sync.ops), where=label
            )
        # Idempotent re-ingestion applies 0 new ops.
        plane = CrdtPlaneRuntime()
        plane.apply_frame(sync)
        n2 = plane.apply_frame(sync)
        assert n2 == 0, f"{frame['label']}: idempotent redelivery applied {n2}"
