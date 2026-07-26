"""The present set plus its authoritative key order, with the atomic-move
algebra (``#lzcellmove``).

This is the **graph-agnostic** half of every ``ReactiveMap`` flavor. It holds no
context, no factory, and no closure: only ``K -> handle`` bookkeeping and the key
list. That is exactly why ordering and atomic move bind the single-threaded,
thread-safe, and async flavors alike — a move touches no entry handle and awaits
nothing, so it is neither thread- nor async-coloured.

What is deliberately **not** here is reactivity. Membership and order
*invalidation* is a graph write, and each flavor must mint its own version cells
on its own graph; a shared core cannot supply them. Each flavor keeps a thin
shell holding this core, its own lock (if any), its own signals, and its own
``materialize`` / ``observe``.

``entries`` and ``order`` stay in lockstep: every key in ``entries`` appears
exactly once in ``order`` and vice versa, including on every failure path.
Reordering here cannot fail — it is a pop + insert on an existing list with both
ends clamped — so there is no allocating error path to desync on.

Rust reference: ``lazily-rs/src/keyed_order.rs``.
"""

from __future__ import annotations

from enum import Enum


__all__ = ["KeyedOrder", "MapMove", "MapMutation"]


class MapMutation(Enum):
    """What a present-set mutation did, so the caller knows what to bump.

    A no-op must bump nothing: bumping on a warm insert would invalidate every
    ``len`` / ``contains_key`` reader on a pure cache hit.
    """

    NONE = "none"
    INSERTED = "inserted"
    REMOVED = "removed"

    @property
    def changed(self) -> bool:
        return self is not MapMutation.NONE


class MapMove(Enum):
    """What an ordering move did.

    ``MISSING`` and ``UNCHANGED`` are distinct because the public ``move_*``
    methods report ``False`` for a missing key but ``True`` for a no-op move —
    while neither may bump the order signal.
    """

    MISSING = "missing"
    UNCHANGED = "unchanged"
    REORDERED = "reordered"

    @property
    def applied(self) -> bool:
        """Whether the move happened at all (the ``bool`` the API returns)."""
        return self is not MapMove.MISSING

    @property
    def changed(self) -> bool:
        """Whether the order actually changed, i.e. whether to bump."""
        return self is MapMove.REORDERED


class KeyedOrder[K, H]:
    """The present set + key order + the move algebra. Closure-free."""

    __slots__ = ("_entries", "_order")

    def __init__(self) -> None:
        self._entries: dict[K, H] = {}
        self._order: list[K] = []

    # -- reads (no graph involvement) ----------------------------------- #

    def get(self, key: K) -> H | None:
        return self._entries.get(key)

    def contains(self, key: K) -> bool:
        return key in self._entries

    def keys(self) -> list[K]:
        """A copy of the authoritative key list; the internal list never escapes."""
        return list(self._order)

    def length(self) -> int:
        return len(self._order)

    def position(self, key: K) -> int | None:
        try:
            return self._order.index(key)
        except ValueError:
            return None

    # -- present-set mutations ------------------------------------------ #

    def insert(self, key: K, handle: H) -> tuple[H, MapMutation]:
        """Append ``handle`` under ``key``.

        A warm key keeps its existing handle (cell-identity: a key's node is
        stable for its lifetime) and reports ``NONE`` so the caller bumps
        nothing.
        """
        existing = self._entries.get(key)
        if existing is not None:
            return existing, MapMutation.NONE
        self._entries[key] = handle
        self._order.append(key)
        return handle, MapMutation.INSERTED

    def remove(self, key: K) -> tuple[H | None, MapMutation]:
        """Drop ``key``, returning its handle so the caller can dispose the node
        on its own graph. The core never touches a handle."""
        handle = self._entries.pop(key, None)
        if handle is None:
            return None, MapMutation.NONE
        self._order = [existing for existing in self._order if existing != key]
        return handle, MapMutation.REMOVED

    # -- the move algebra ------------------------------------------------ #

    def move_to(self, key: K, index: int) -> MapMove:
        """Move ``key`` to ``index``, clamped to ``[0, len)``.

        The entry keeps the same handle, its dependents, and its CRDT lineage —
        that is what separates a reorder from a remove + re-mint.

        Both ends are clamped. A negative index used to reach Python's
        insert-from-the-right semantics, so ``move_to(key, -1)`` landed the key
        second-to-last rather than first — the same missing lower clamp
        lazily-js's ``reactive-family.js`` has.
        """
        from_pos = self.position(key)
        if from_pos is None:
            return MapMove.MISSING
        to = max(0, min(index, len(self._order) - 1))
        if from_pos == to:
            return MapMove.UNCHANGED
        self._order.pop(from_pos)
        self._order.insert(to, key)
        return MapMove.REORDERED

    def move_before(self, key: K, anchor: K) -> MapMove:
        """Move ``key`` to just before ``anchor``.

        The target is computed on the **pre-removal** list: when ``key``
        currently precedes ``anchor``, lifting it out shifts ``anchor`` one slot
        left, so the insertion point is ``anchor - 1``. Getting this wrong lands
        the key on the far side of its anchor — the defect found in lazily-zig,
        where ``move_before("a", "d")`` on ``[a,b,c,d]`` produced ``[b,c,d,a]``.
        """
        anchor_idx = self.position(anchor)
        from_pos = self.position(key)
        if anchor_idx is None or from_pos is None:
            return MapMove.MISSING
        target = anchor_idx - 1 if from_pos < anchor_idx else anchor_idx
        return self.move_to(key, target)

    def move_after(self, key: K, anchor: K) -> MapMove:
        """Move ``key`` to just after ``anchor``. Same pre-removal reasoning."""
        anchor_idx = self.position(anchor)
        from_pos = self.position(key)
        if anchor_idx is None or from_pos is None:
            return MapMove.MISSING
        target = anchor_idx if from_pos <= anchor_idx else anchor_idx + 1
        return self.move_to(key, target)
