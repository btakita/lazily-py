"""Property and unit tests for the new feature modules.

These mirror the named invariants from ``lazily-spec`` / ``lazily-formal`` and
exercise the surface that the conformance fixtures do not cover directly.
"""

from __future__ import annotations

from lazily import (
    CellCrdt,
    CrdtOp,
    CrdtPlaneRuntime,
    LwwRegister,
    MvRegister,
    PnCounter,
    SemTree,
    SeqCrdt,
    SourceMap,
    StateMirror,
    TextCrdt,
    WireStamp,
)
from lazily.ipc import IpcValue_Inline


# ---------------------------------------------------------------------------
# CRDT registers
# ---------------------------------------------------------------------------


def test_lww_register_last_writer_wins() -> None:
    r = LwwRegister[int]()
    r.assign(1, WireStamp(10, 0, 1))
    assert r.value == 1
    # Older stamp loses.
    r2 = LwwRegister[int](value=2, stamp=WireStamp(5, 0, 2))
    assert not r.merge(r2)
    assert r.value == 1
    # Newer stamp wins.
    r3 = LwwRegister[int](value=3, stamp=WireStamp(12, 0, 2))
    assert r.merge(r3)
    assert r.value == 3


def test_lww_register_peer_tiebreak() -> None:
    # Same (wall, logical): higher peer wins.
    a = LwwRegister[int](value=1, stamp=WireStamp(5, 3, 1))
    b = LwwRegister[int](value=2, stamp=WireStamp(5, 3, 2))
    assert a.merge(b)
    assert a.value == 2


def test_lww_register_idempotent_merge() -> None:
    a = LwwRegister[int](value=1, stamp=WireStamp(10, 0, 1))
    b = LwwRegister[int](value=2, stamp=WireStamp(12, 0, 2))
    assert a.merge(b)
    assert not a.merge(b)  # idempotent


def test_mv_register_surfaces_concurrent_writes() -> None:
    a = MvRegister[int]()
    a.write(1, WireStamp(5, 0, 1))
    b = MvRegister[int]()
    b.write(2, WireStamp(5, 0, 2))
    a.merge(b)
    values = sorted(a.values())
    assert values == [1, 2]


def test_pn_counter_merge_converges() -> None:
    a = PnCounter()
    a.increment(1, 3)
    a.decrement(1, 1)
    b = PnCounter()
    b.increment(2, 5)
    b.decrement(2, 2)
    a.merge(b)
    assert a.value() == 5
    # Idempotent re-merge.
    assert not a.merge(b)
    assert a.value() == 5


def test_cell_crdt_propagates_merge_into_reactive_cell() -> None:
    ctx: dict = {}
    cell = CellCrdt[bytes](ctx)
    cell.assign(b"v1", WireStamp(1, 0, 1))
    assert cell.value == b"v1"
    # Merge a newer remote state.
    other = LwwRegister[bytes](value=b"v2", stamp=WireStamp(2, 0, 2))
    assert cell.merge(other)
    assert cell.value == b"v2"
    # PartialEq guard: equal value doesn't fire a second propagation.
    assert not cell.merge(other)


# ---------------------------------------------------------------------------
# SemTree incremental + memo guard
# ---------------------------------------------------------------------------


def test_semtree_edit_recomputes_only_ancestor_chain() -> None:
    tree = SemTree[str, int](fold="sum")
    # root -> [a -> [a1, a2], b -> [b1]]
    tree.add("root", 0)
    tree.add("a", 1)
    tree.insert_child("root", "a", 1)
    tree.add("b", 2)
    tree.insert_child("root", "b", 2)
    tree.add("a1", 10)
    tree.insert_child("a", "a1", 10)
    tree.add("a2", 20)
    tree.insert_child("a", "a2", 20)
    tree.add("b1", 100)
    tree.insert_child("b", "b1", 100)
    assert tree.derived("root") == 133
    for n in tree._nodes.values():
        n.compute_count = 0
    tree.set_node_value("b1", 200)
    assert tree.derived("root") == 233
    # Sibling 'a' chain was not recomputed.
    assert tree.node("a").compute_count == 0
    assert tree.node("a1").compute_count == 0
    # Ancestor chain of b1 WAS recomputed.
    assert tree.node("b").compute_count > 0


def test_semtree_memo_guard_suppresses_equal_recompute() -> None:
    tree = SemTree[str, int](fold="count_positive")
    tree.add("root", 0)
    tree.add("a", -1)
    tree.insert_child("root", "a", -1)
    tree.add("b", 7)
    tree.insert_child("root", "b", 7)
    assert tree.derived("root") == 1
    root = tree.node("root")
    root.downstream_count = 0
    # b changes 7 -> 9: both positive, count stays 1 → memo guard suppresses.
    tree.set_node_value("b", 9)
    tree.derived("root")
    assert root.derived == 1
    assert root.downstream_count == 0


# ---------------------------------------------------------------------------
# TextCrdt properties
# ---------------------------------------------------------------------------


def test_textcrdt_merge_commutative() -> None:
    a = TextCrdt.seed(1, "ab")
    b = TextCrdt.seed(2, "cd")
    m1 = a.clone()
    m1.merge(b)
    m2 = b.clone()
    m2.merge(a)
    assert m1.text() == m2.text()


def test_textcrdt_merge_associative() -> None:
    a = TextCrdt.seed(1, "x")
    b = TextCrdt.seed(2, "y")
    c = TextCrdt.seed(3, "z")
    # (a merged with b) merged with c == a merged with (b merged with c)
    left = a.clone()
    left.merge(b)
    left.merge(c)
    right = a.clone()
    bc = b.clone()
    bc.merge(c)
    right.merge(bc)
    assert left.text() == right.text()


def test_textcrdt_repeated_delete_is_idempotent() -> None:
    a = TextCrdt.seed(1, "abc")
    a.delete(1)
    text_after = a.text()
    # Re-applying the same tombstones via merge is a no-op.
    snapshot = a.clone()
    a.merge(snapshot)
    assert a.text() == text_after


def test_textcrdt_version_vector_covers_tombstones() -> None:
    a = TextCrdt.seed(1, "abc")
    a.delete(1)
    vv = a.version_vector()
    # delete id is counter 4 at peer 1.
    assert vv == {1: 4}


def test_textcrdt_delta_apply_idempotent() -> None:
    a = TextCrdt.seed(1, "hello")
    b = TextCrdt(2)
    delta = a.delta_since({})
    assert b.apply_delta(delta)
    assert not b.apply_delta(delta)  # idempotent
    assert a.text() == b.text()


# ---------------------------------------------------------------------------
# SeqCrdt properties
# ---------------------------------------------------------------------------


def test_seqcrdt_move_is_single_reassignment_no_duplication() -> None:
    r = SeqCrdt(1)
    r.insert_back("a", 0, 1)
    r.insert_back("b", 1, 2)
    r.insert_back("c", 2, 3)
    r.move_after("a", "c", 10)
    assert len(r) == 3
    assert r.order() == ["b", "c", "a"]


def test_seqcrdt_concurrent_move_and_value_edit_both_apply() -> None:
    a = SeqCrdt(1)
    a.insert_back("a", 1, 1)
    a.insert_back("b", 2, 2)
    b = a.clone()
    b.peer = 2
    a.move_after("a", "b", 10)
    b.set_value("a", 99, 10)
    merged = a.clone()
    merged.merge(b)
    assert merged.order() == ["b", "a"]
    assert merged.get("a") == 99


def test_seqcrdt_concurrent_moves_converge_to_later_stamp() -> None:
    a = SeqCrdt(1)
    for k, v, n in [("x", "X", 1), ("y", "Y", 2), ("z", "Z", 3)]:
        a.insert_back(k, v, n)
    b = a.clone()
    b.peer = 2
    a.move_after("x", "y", 10)
    b.move_after("x", "z", 20)
    merged = a.clone()
    merged.merge(b)
    assert merged.order() == ["y", "z", "x"]


def test_seqcrdt_remove_is_lww_tombstone() -> None:
    a = SeqCrdt(1)
    a.insert_back("a", 1, 1)
    a.insert_back("b", 2, 2)
    a.remove("b", 10)
    assert "b" not in a
    assert a.order() == ["a"]


# ---------------------------------------------------------------------------
# CrdtPlaneRuntime frontier / watermark
# ---------------------------------------------------------------------------


def test_plane_frontier_and_watermark() -> None:
    plane = CrdtPlaneRuntime()
    plane.apply(CrdtOp.new(1, WireStamp(10, 0, 1), IpcValue_Inline(b"a")))
    plane.apply(CrdtOp.new(2, WireStamp(5, 0, 2), IpcValue_Inline(b"b")))
    frontier = dict(plane.frontier())
    assert frontier[1] == WireStamp(10, 0, 1)
    assert frontier[2] == WireStamp(5, 0, 2)
    wm = plane.stability_watermark()
    assert wm == WireStamp(5, 0, 2)


def test_plane_to_sync_round_trips_through_crdt_sync() -> None:
    from lazily import IpcMessage

    plane = CrdtPlaneRuntime()
    plane.apply(CrdtOp.new(1, WireStamp(10, 0, 1), IpcValue_Inline(b"x")))
    sync = plane.to_sync()
    msg = IpcMessage.of_crdt_sync(sync)
    wire = msg.encode_json()
    decoded = IpcMessage.decode_json(wire)
    assert decoded.crdt_sync == sync


def test_plane_delta_sync_only_sends_unobserved() -> None:
    plane = CrdtPlaneRuntime()
    plane.apply(CrdtOp.new(1, WireStamp(10, 0, 1), IpcValue_Inline(b"a")))
    plane.apply(CrdtOp.new(1, WireStamp(20, 0, 1), IpcValue_Inline(b"b")))
    # Partner has observed peer 1 up to stamp 10.
    delta = plane.delta_sync([(1, WireStamp(10, 0, 1))])
    assert len(delta.ops) == 1
    assert delta.ops[0].stamp == WireStamp(20, 0, 1)


# ---------------------------------------------------------------------------
# StateMirror
# ---------------------------------------------------------------------------


def test_state_mirror_snapshot_and_delta() -> None:
    m = StateMirror()
    m.track_cell(1, "name", b"World")
    m.track_slot(2, "greeting", b"Hello!")
    m.add_edge(2, 1)
    snap = m.snapshot()
    assert snap.epoch == 0
    assert {n.node for n in snap.nodes} == {1, 2}
    assert snap.roots == [1]
    m.publish_cell(1, b"Lazily")
    m.invalidate_slot(2, b"Hello, Lazily!")
    delta = m.flush_and_bump()
    kinds = [type(o).__name__ for o in delta.ops]
    assert "DeltaOp_CellSet" in kinds
    assert "DeltaOp_SlotValue" in kinds
    assert delta.base_epoch == 0
    assert delta.epoch == 1


def test_state_mirror_permission_filters_snapshot() -> None:
    from lazily import PeerPermissions, RemoteOp

    perms = PeerPermissions()
    perms.allow(7, RemoteOp.read(1))
    m = StateMirror(permissions=perms)
    m.track_cell(1, "name", b"World")
    m.track_cell(2, "secret", b"x")
    snap = m.snapshot_for(7)
    assert {n.node for n in snap.nodes} == {1}


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_benchmarks_run_and_report() -> None:
    from lazily import run_benchmarks

    results = run_benchmarks(samples=50)
    assert len(results) >= 5
    for r in results:
        assert r.samples == 50
        assert r.per_op_seconds >= 0.0
        assert r.name


# ---------------------------------------------------------------------------
# SemTree.from_json + SourceMap sanity (regression)
# ---------------------------------------------------------------------------


def test_source_map_atomic_move_preserves_identity() -> None:
    ctx: dict = {}
    cmap = SourceMap[int, int](ctx)
    cmap.entry(1, 10)
    cell_before = cmap.value_cell(1)
    cmap.move_to(1, 0)
    assert cmap.value_cell(1) is cell_before  # identity preserved
    assert cmap.value_cell(1).value == 10
