"""Ordered keyed reactive tree — SourceTree per-node / per-level laws.

The Python counterpart of the Lean ``LazilyFormal.Tree`` formal model in
``lazily-formal``. Each test mirrors a named theorem (per-node value reactivity,
per-level membership/order reactivity, atomic-move identity preservation).
"""

from __future__ import annotations

from lazily import SourceTree


# =================================================================================
# setNodeValue_preserves_{other_nodes,node_signals}
# Editing one node's value cannot disturb any other node, nor the edited node's
# own child collection / per-level signals.
# =================================================================================


def test_set_node_value_preserves_other_nodes() -> None:
    ctx: dict = {}
    t = SourceTree[int, int](ctx)
    t.add(1, 10)
    t.add(2, 20)
    t.insert_child(1, 11, 111)
    t.set_node_value(1, 999)

    assert t.value_of(2) == 20  # sibling node untouched
    assert t.children_of(1) == [11]  # node 1's child collection untouched


def test_set_node_value_preserves_node_signals() -> None:
    ctx: dict = {}
    t = SourceTree[int, int](ctx)
    t.add(1, 10)
    t.insert_child(1, 11, 111)
    node1 = t.node(1)
    assert node1 is not None
    m_before = node1.membership_signal.value
    o_before = node1.order_signal.value

    t.set_node_value(1, 999)

    node1b = t.node(1)
    assert node1b is not None
    assert node1b.membership_signal.value == m_before  # unchanged
    assert node1b.order_signal.value == o_before  # unchanged
    assert t.value_of(1) == 999


# =================================================================================
# moveChild_preserves_{non_parent,parent_value} / moveChild_advances_order_signal_only
# A pure reorder changes no value cell anywhere; only the parent's order signal
# bumps. The child keeps its identity (not remove + re-mint).
# =================================================================================


def test_move_child_preserves_non_parent() -> None:
    ctx: dict = {}
    t = SourceTree[int, int](ctx)
    t.add(1, 10)
    t.add(2, 20)
    t.insert_child(1, 11, 111)
    t.insert_child(1, 12, 122)
    t.insert_child(2, 21, 211)

    t.move_child(1, 12, 0)  # reorder under node 1

    assert t.value_of(2) == 20  # node 2 untouched (non-parent)
    node2 = t.node(2)
    assert node2 is not None
    assert node2.children == [21]  # node 2's child collection untouched
    assert t.children_of(1) == [12, 11]  # node 1 reordered


def test_move_child_preserves_parent_value() -> None:
    ctx: dict = {}
    t = SourceTree[int, int](ctx)
    t.add(1, 10)
    t.insert_child(1, 11, 111)
    t.insert_child(1, 12, 122)
    t.move_child(1, 11, 1)
    assert t.value_of(1) == 10  # parent's own value untouched


def test_move_child_advances_order_signal_only() -> None:
    ctx: dict = {}
    t = SourceTree[int, int](ctx)
    t.add(1, 10)
    t.insert_child(1, 11, 111)
    t.insert_child(1, 12, 122)
    node1 = t.node(1)
    assert node1 is not None
    m_before = node1.membership_signal.value
    o_before = node1.order_signal.value

    t.move_child(1, 11, 1)

    node1b = t.node(1)
    assert node1b is not None
    assert node1b.membership_signal.value == m_before  # unchanged
    assert node1b.order_signal.value == o_before + 1  # advanced exactly once
    assert node1b.value_cell.value == 10  # value untouched
    assert t.children_of(1) == [12, 11]


def test_insert_child_advances_membership_and_order() -> None:
    ctx: dict = {}
    t = SourceTree[int, int](ctx)
    t.add(1, 10)
    node1 = t.node(1)
    assert node1 is not None
    m0 = node1.membership_signal.value
    o0 = node1.order_signal.value

    t.insert_child(1, 11, 111)

    node1b = t.node(1)
    assert node1b is not None
    assert node1b.membership_signal.value == m0 + 1
    assert node1b.order_signal.value == o0 + 1
    assert t.value_of(1) == 10  # parent value untouched
