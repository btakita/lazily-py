"""Ordered keyed reactive tree — ``SourceTree``.

The Python counterpart of the Lean ``LazilyFormal.Tree`` formal model in
``lazily-formal`` and ``lazily-spec/cell-model.md`` § "Ordered keyed tree".
Each tree node is ``(stable id, value cell, ordered keyed child collection)``;
the model fixes the per-node / per-level reactivity independence laws:

- **per-node value reactivity** — editing one node's value invalidates only that
  node's readers — never a sibling, a child, or an ancestor;
- **per-level membership/order reactivity** — each node carries its own
  independent membership/order signals for its child collection, so a sibling
  subtree change does not invalidate an unrelated level's child readers;
- **atomic move preserves identity** — a child reorder keeps the child's cell
  identity and value (not remove + re-mint), bumping only the parent's order
  signal once.

The tree is a **composition of cells** — not a new cell kind — so the per-cell
merge model of :mod:`lazily.collection` and :mod:`lazily.cell` applies
node-by-node.
"""

from __future__ import annotations


__all__ = ["CellTree", "SourceTree", "TreeNode"]

from typing import TypeVar

from .cell import Cell


N = TypeVar("N")
V = TypeVar("V")


class TreeNode[N, V]:
    """One tree node: a value cell plus an ordered keyed child collection
    carrying its own independent per-level membership and order signals.

    Mirrors ``LazilyFormal.Tree.TreeNode``.
    """

    __slots__ = ("_ctx", "children", "membership_signal", "order_signal", "value_cell")

    def __init__(self, ctx: dict, value: V) -> None:
        self._ctx = ctx
        self.value_cell: Cell[V] = Cell(ctx, value)
        self.children: list[N] = []
        self.membership_signal: Cell[int] = Cell(ctx, 0)
        self.order_signal: Cell[int] = Cell(ctx, 0)

    @property
    def value(self) -> V:
        return self.value_cell.value


class SourceTree[N, V]:
    """A reactive tree: nodes keyed by stable id. Each node's per-level
    reactivity is independent of every other node's.

    Mirrors ``lazily-rs/src/cell_tree.rs`` (``SourceTree<Id, V>``) and the Lean
    ``LazilyFormal.Tree`` model.
    """

    __slots__ = ("_nodes", "ctx")

    def __init__(self, ctx: dict) -> None:
        self.ctx = ctx
        self._nodes: dict[N, TreeNode[N, V]] = {}

    # -- reads ---------------------------------------------------------- #

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def node(self, node_id: N) -> TreeNode[N, V] | None:
        return self._nodes.get(node_id)

    def value_of(self, node_id: N) -> V | None:
        t = self._nodes.get(node_id)
        return t.value_cell.value if t is not None else None

    def children_of(self, node_id: N) -> list[N]:
        t = self._nodes.get(node_id)
        if t is None:
            return []
        _ = t.order_signal.value
        return list(t.children)

    def contains_child(self, parent: N, child: object) -> bool:
        t = self._nodes.get(parent)
        if t is None:
            return False
        _ = t.membership_signal.value
        return child in t.children

    # -- mutators ------------------------------------------------------- #

    def add(self, node_id: N, value: V) -> None:
        """Add a new rootless node (mint its value cell). Idempotent."""
        if node_id not in self._nodes:
            self._nodes[node_id] = TreeNode(self.ctx, value)

    def set_node_value(self, node_id: N, value: V) -> None:
        """Edit node ``node_id``'s value, leaving its children and per-level
        signals — and every other node — untouched. Mirrors ``setNodeValue``."""
        t = self._nodes.get(node_id)
        if t is not None:
            t.value_cell.set(value)

    def insert_child(self, parent: N, child: N, value: V) -> None:
        """Insert ``child`` as a new member of ``parent``'s child collection,
        first creating ``child`` as a node if needed. Bumps ``parent``'s
        membership **and** order signal; leaves every other node (and
        ``parent``'s own value) untouched. Mirrors ``insertChild``."""
        p = self._nodes.get(parent)
        if p is None:
            return
        if child not in self._nodes:
            self._nodes[child] = TreeNode(self.ctx, value)
        if child in p.children:
            return
        p.children.append(child)
        p.membership_signal.set(p.membership_signal.value + 1)
        p.order_signal.set(p.order_signal.value + 1)

    def move_child(self, parent: N, child: N, index: int) -> None:
        """A pure reorder within ``parent``: move ``child`` to position
        ``index``. Bumps **only** ``parent``'s order signal; membership, every
        value cell, and every other node are untouched. The child keeps its
        identity (not remove + re-mint). Mirrors ``moveChild``."""
        p = self._nodes.get(parent)
        if p is None or child not in p.children:
            return
        p.children = [c for c in p.children if c != child]
        clamped = min(index, len(p.children))
        p.children.insert(clamped, child)
        p.order_signal.set(p.order_signal.value + 1)


# ---------------------------------------------------------------------------
# Deprecated aliases (v2 kernel rename)
# ---------------------------------------------------------------------------
# The v2 kernel renamed the node kinds to ``Source`` and ``Computed``; the tree
# type still said ``Cell``, a vocabulary the kernel no longer uses. Its nodes
# hold input cells, so ``SourceTree`` says which kind of entry it holds — the
# same move that produced ``SourceMap`` / ``ComputedMap``.
#
# The old name stays as a plain alias so existing imports keep working — a
# rename is not a removal. Importing it from the ``lazily`` package root emits a
# :class:`DeprecationWarning` (see ``lazily/__init__.py``); the submodule alias
# below is the un-warned escape hatch.

#: Deprecated alias of :class:`SourceTree`.
CellTree = SourceTree
