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

from conformance_assert import instrument

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
_SPEC = Path(__file__).resolve().parents[2] / "lazily-spec" / "conformance"


def _load(rel: str) -> dict:
    path = _SPEC / rel
    if not path.exists():
        path = _LOCAL / rel
    return instrument(json.loads(path.read_text()), name=rel)


# ---------------------------------------------------------------------------
# Stable-id alignment (manufactured identity)
# ---------------------------------------------------------------------------


def test_stableid_alignment_conformance() -> None:
    fix = _load("collections/stableid_alignment.json")
    assert fix["model"] == "StableId"
    for sc in fix["scenarios"]:
        name = sc["name"]
        if "blocks" in sc:
            # key-equality scenarios
            blocks = sc["blocks"]
            keys = [block_key(b) for b in blocks]
            for i, j in sc["expect"].get("key_equal", []):
                assert keys[i] == keys[j], f"{name}: key_equal [{i},{j}]"
            for i, j in sc["expect"].get("key_not_equal", []):
                assert keys[i] != keys[j], f"{name}: key_not_equal [{i},{j}]"
            continue
        if "new_key_equals_old_key" in sc["expect"]:
            keys = assign_stable_keys(sc["old"], sc["new"])
            for ni, oi in sc["expect"]["new_key_equals_old_key"]:
                assert keys[ni] == block_key(sc["old"][oi]), (
                    f"{name}: new[{ni}] key != old[{oi}] key"
                )
            continue
        if "old" in sc and "new" in sc:
            a = align(sc["old"], sc["new"])
            assert a.matches == sc["expect"]["matches"], f"{name}: matches"
            assert a.removed == sc["expect"]["removed"], f"{name}: removed"
            if "similarity_min" in sc["expect"]:
                lo = sc["expect"]["similarity_min"]
                sim = similarity(sc["old"][0]["text"], sc["new"][0]["text"])
                assert sim >= lo, f"{name}: similarity {sim} < {lo}"


# ---------------------------------------------------------------------------
# Memoized semantic tree
# ---------------------------------------------------------------------------


def test_semtree_conformance() -> None:
    fix = _load("collections/semtree_incremental.json")
    assert fix["model"] == "SemTree"
    for sc in fix["scenarios"]:
        tree: SemTree[str, int] = SemTree.from_json(sc["tree"], fold=sc["fold"])  # type: ignore[arg-type]
        expect_initial = sc["expect_initial"]
        for node_id, want in expect_initial.items():
            if node_id.startswith("sibling_") or node_id.startswith("downstream_"):
                continue
            assert tree.derived(node_id) == want, (
                f"{sc['name']}: initial derived({node_id})={tree.derived(node_id)} want {want}"
            )
        # Reset the recomputation counters after the warm-up so the edit-phase
        # counts measure ONLY the edit's effect.
        for node in tree._nodes.values():
            node.compute_count = 0
            node.downstream_count = 0
        if "edit" in sc:
            tree.set_node_value(sc["edit"]["id"], sc["edit"]["value"])
            for node_id, want in sc["expect_after"].items():
                if node_id == "sibling_a_cached":
                    # An edit to b1 must not recompute the sibling subtree 'a'.
                    assert tree.node("a").compute_count == 0, (
                        f"{sc['name']}: sibling 'a' was recomputed (not cached)"
                    )
                    continue
                if node_id == "downstream_consumer_reran":
                    assert tree.node("root").downstream_count == 0, (
                        f"{sc['name']}: downstream consumer re-ran (memo guard failed)"
                    )
                    continue
                assert tree.derived(node_id) == want, (
                    f"{sc['name']}: after edit derived({node_id})={tree.derived(node_id)} want {want}"
                )
        if "remove_child" in sc:
            tree.remove_child(sc["remove_child"]["parent"], sc["remove_child"]["child"])
            for node_id, want in sc["expect_after"].items():
                assert tree.derived(node_id) == want, (
                    f"{sc['name']}: after remove derived({node_id})={tree.derived(node_id)} want {want}"
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

    def _op(self, target: TextCrdt, st: dict) -> None:
        op = st["op"]
        if op == "insert":
            target.insert(st["index"], st["ch"])
        elif op == "insert_str":
            target.insert_str(st["index"], st["str"])
        elif op == "delete":
            target.delete(st["index"])


def test_textcrdt_convergence_conformance() -> None:
    fix = _load("collections/textcrdt_convergence.json")
    assert fix["model"] == "TextCrdt"
    for sc in fix["scenarios"]:
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
        if "text" in exp:
            assert interp.r["a"].text() == exp["text"], sc["name"]
        if "len" in exp:
            assert len(interp.r["a"]) == exp["len"], sc["name"]
        if "tombstone_count" in exp:
            assert interp.r["a"].tombstone_count() == exp["tombstone_count"], sc["name"]
        for pair in exp.get("texts_equal", []):
            assert interp.r[pair[0]].text() == interp.r[pair[1]].text(), sc["name"]
        if "a_starts_with" in exp:
            assert interp.r["a"].text().startswith(exp["a_starts_with"]), sc["name"]
        if "a_ends_with" in exp:
            assert interp.r["a"].text().endswith(exp["a_ends_with"]), sc["name"]


def test_textcrdt_delta_sync_conformance() -> None:
    fix = _load("collections/textcrdt_delta_sync.json")
    assert fix["model"] == "TextCrdt"
    for sc in fix["scenarios"]:
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
        exp = sc["expect"]
        for pair in exp.get("texts_equal", []):
            assert interp.r[pair[0]].text() == interp.r[pair[1]].text(), sc["name"]
        for who, want in exp.get("text_on", {}).items():
            assert interp.r[who].text() == want, f"{sc['name']}: text_on {who}"
        for who, want in exp.get("version_vector_on", {}).items():
            vv = interp.r[who].version_vector()
            want_vv = {int(k): v for k, v in want.items()}
            assert vv == want_vv, f"{sc['name']}: vv_on {who} got {vv} want {want_vv}"


# ---------------------------------------------------------------------------
# SeqCrdt convergence
# ---------------------------------------------------------------------------


class _SeqReplicas:
    def __init__(self) -> None:
        self.r: dict[str, SeqCrdt] = {}

    def run(self, steps: list[dict]) -> None:
        for st in steps:
            if "fork" in st:
                src = self.r.get("a")
                self.r[st["fork"]] = (
                    src.clone() if src is not None else SeqCrdt(st["peer"])
                )
                self.r[st["fork"]].peer = st["peer"]
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


def test_seqcrdt_convergence_conformance() -> None:
    fix = _load("collections/seqcrdt_convergence.json")
    assert fix["model"] == "SeqCrdt"
    for sc in fix["scenarios"]:
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
        if "order" in exp:
            assert interp.r["a"].order() == exp["order"], sc["name"]
        if "len" in exp:
            for who in len_targets:
                assert len(interp.r[who]) == exp["len"], (
                    f"{sc['name']}: len({who})={len(interp.r[who])} want {exp['len']}"
                )
        if "get" in exp:
            for k, v in exp["get"].items():
                assert interp.r["a"].get(k) == v, f"{sc['name']}: get({k})"
        for who, want in exp.get("order_on", {}).items():
            assert interp.r[who].order() == want, f"{sc['name']}: order_on {who}"
        for who, gets in exp.get("get_on", {}).items():
            for k, v in gets.items():
                assert interp.r[who].get(k) == v, f"{sc['name']}: get_on {who} {k}"
        for pair in exp.get("orders_equal", []):
            assert interp.r[pair[0]].order() == interp.r[pair[1]].order(), sc["name"]
        for who, items in exp.get("not_contains_on", {}).items():
            for item in items:
                assert item not in interp.r[who], (
                    f"{sc['name']}: not_contains_on {who} {item}"
                )
        if "contains_all" in exp:
            for item in exp["contains_all"]:
                for who in len_targets:
                    assert item in interp.r[who], (
                        f"{sc['name']}: contains {item} on {who}"
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
    for sc in fix["scenarios"]:
        expect = sc["expect"]
        plane = CrdtPlaneRuntime()
        applied = plane.apply_ops([_mk_crdtop(o) for o in sc["ops"]])
        assert applied == expect["applied_count"], sc["name"]

        # `resolution`: the winner is the op with the greatest WireStamp, not
        # the last one delivered. Asserting the converged state alone cannot
        # tell those apart when they coincide, so pin the rule itself.
        assert expect["resolution"] == "max_stamp", sc["name"]
        by_node: dict[int, dict] = {}
        for op in sc["ops"]:
            stamp = op["stamp"]
            key = (stamp["wall_time"], stamp["logical"], stamp["peer"])
            best = by_node.get(op["node"])
            if best is None or key > best["_key"]:
                by_node[op["node"]] = {**op, "_key": key}
        for entry in plane.converged():
            winner = by_node[entry.node]
            assert entry.state == bytes(winner["state"]["Inline"]), (
                f"{sc['name']}: node {entry.node} did not resolve to the max stamp"
            )

        if sc.get("redeliver"):
            rd = plane.apply_ops([_mk_crdtop(o) for o in sc["ops"]])
            assert rd == expect["redeliver_applied_count"], sc["name"]
        if sc.get("reverse_order_equivalent"):
            plane2 = CrdtPlaneRuntime()
            plane2.apply_ops([_mk_crdtop(o) for o in reversed(sc["ops"])])
            got = {(e.node, e.state) for e in plane.converged()}
            got2 = {(e.node, e.state) for e in plane2.converged()}
            assert (got == got2) is expect["order_independent"], (
                f"{sc['name']}: reverse order not equivalent"
            )
        for want in expect["converged"]:
            matches = [
                e
                for e in plane.converged()
                if e.node == want["node"]
                and (
                    (e.key is None and not want.get("key"))
                    or (e.key is not None and e.key.path == want.get("key"))
                )
            ]
            assert matches, f"{sc['name']}: no converged entry for node {want['node']}"
            assert matches[0].state == bytes(want["state"]["Inline"]), (
                f"{sc['name']}: converged state mismatch for node {want['node']}"
            )


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
        if "frontier_len" in a:
            assert len(sync.frontier) == a["frontier_len"], frame["label"]
        if "frontier_omitted" in a:
            # #lzspecfrontiersuppress: an omitted frontier decodes as empty.
            assert a["frontier_omitted"] is True, frame["label"]
            assert "frontier" not in wire["CrdtSync"], frame["label"]
            assert sync.frontier == [], frame["label"]
        assert len(sync.ops) == a["op_count"], frame["label"]
        if "has_keyed_op" in a:
            assert any(op.key is not None for op in sync.ops), frame["label"]
        if "has_keyless_op" in a:
            assert any(op.key is None for op in sync.ops), frame["label"]
        # Idempotent re-ingestion applies 0 new ops.
        plane = CrdtPlaneRuntime()
        plane.apply_frame(sync)
        n2 = plane.apply_frame(sync)
        assert n2 == 0, f"{frame['label']}: idempotent redelivery applied {n2}"
