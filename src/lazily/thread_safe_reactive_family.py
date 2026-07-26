"""Thread-safe keyed reactive collection (``#reactivemap``, thread-safe flavor) —
the :class:`~lazily.ThreadSafeContext` analog of :class:`~lazily.ReactiveMap`.

The Python counterpart of ``lazily-rs/src/thread_safe_reactive_family.rs``, the
Lean ``LazilyFormal.Materialization`` confluence theorems in ``lazily-formal``,
and ``lazily-spec/cell-model.md`` § "Execution-context flavors".

Keys ``K`` map to per-entry reactive nodes (:class:`~lazily.Cell` inputs /
:class:`~lazily.slot` derived nodes) whose writes are serialized through an
owning :class:`~lazily.ThreadSafeContext`. The present-set state is guarded by its
own lock so a keyed map can be materialized concurrently from multiple threads.

Its two specializations are :class:`ThreadSafeSourceMap` (input cells) and
:class:`ThreadSafeComputedMap` (derived slots). Eager materialization is a pre-mint
loop (:meth:`ThreadSafeComputedMap.materialize_all`); lazy is mint-on-access
(:meth:`ThreadSafeReactiveMap.get_or_insert_with`) — there is no eager/lazy mode
flag. What the thread-safe flavor adds is **materialization confluence** (proved
in ``lazily-formal`` as ``materialize_present_comm`` / ``materialize_observe_comm``):
:meth:`ThreadSafeReactiveMap._mint_with` computes the node **outside** the lock,
then commits under it **first-writer-wins**, so a raced key keeps a single stable
handle.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, TypeVar

from ._keyed_order import KeyedOrder, MapMove
from .cell import Cell
from .collection import (
    _COMPUTED_HANDLE,
    _SOURCE_HANDLE,
    EntryKind,
    MapHandle,
    _HandleKind,
)
from .thread_safe import ThreadSafeContext


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


__all__ = [
    "ThreadSafeCellMap",
    "ThreadSafeComputedMap",
    "ThreadSafeReactiveMap",
    "ThreadSafeSlotMap",
    "ThreadSafeSourceMap",
]

K = TypeVar("K")
V = TypeVar("V")


class ThreadSafeReactiveMap[K, V]:
    """The thread-safe keyed reactive collection (``#reactivemap``): keys map to
    per-entry reactive nodes on a :class:`~lazily.ThreadSafeContext`. Present-set
    state is guarded by a lock; materialization is confluent under concurrent
    access. Its two specializations are :class:`ThreadSafeSourceMap` (input cells)
    and :class:`ThreadSafeComputedMap` (derived slots).

    Cell entries are written through the owning context's coalescing
    :meth:`~lazily.ThreadSafeContext.set_cell` / :meth:`~lazily.ThreadSafeContext.batch`
    boundary; expose it via :attr:`context`.
    """

    #: The entry handle kind — set by the specialization.
    _HANDLE: _HandleKind = _SOURCE_HANDLE

    __slots__ = (
        "_ctx",
        "_keyed",
        "_membership_signal",
        "_mutex",
        "_order_signal",
        "_order_version",
        "_ts",
        "_version",
    )

    def __init__(self, ctx: dict, *, ts: ThreadSafeContext | None = None) -> None:
        self._ctx = ctx
        self._ts = ts if ts is not None else ThreadSafeContext()
        # Present set + key order + the move algebra, shared with the other two
        # flavors. Graph-agnostic; the reactivity below is this flavor's own.
        self._keyed: KeyedOrder[K, MapHandle] = KeyedOrder()
        # A dedicated present-set lock, separate from the context's write lock, so
        # a slot recompute triggered while committing cannot re-enter it.
        self._mutex = threading.RLock()
        # Membership and order signals minted on THIS flavor's graph. A shared
        # graph-agnostic core cannot supply reactivity.
        self._membership_signal: Cell[int] = Cell(ctx, 0)
        self._order_signal: Cell[int] = Cell(ctx, 0)
        # Untracked mirrors, so a mutator bumps the reactive cell without reading
        # its ``.value`` (which would register a spurious dependency when a mint
        # happens inside a running computation).
        self._version = 0
        self._order_version = 0

    # -- constructors --------------------------------------------------- #

    @classmethod
    def new(
        cls, ctx: dict, *, ts: ThreadSafeContext | None = None
    ) -> ThreadSafeReactiveMap[K, V]:
        """Create an empty map bound to ``ctx``."""
        return cls(ctx, ts=ts)

    # -- internals ------------------------------------------------------ #

    def _mint_with(self, key: K, compute: Callable[[Any], V]) -> MapHandle:
        # Fast path under the present-set lock: return the warm entry if present.
        with self._mutex:
            warm = self._keyed.get(key)
        if warm is not None:
            return warm
        # Build the node OUTSIDE the lock so a slot recompute cannot re-enter it.
        handle = self._HANDLE.materialize(self._ctx, compute)
        # First-writer-wins commit: on a lost race the freshly-built node is
        # orphaned (unreferenced) and the key keeps its single stable handle.
        with self._mutex:
            stored, mutation = self._keyed.insert(key, handle)
        # Bump off the lock: a set can drive a dependent recompute that re-enters
        # this map.
        if mutation.changed:
            self._bump_membership()
        return stored

    # -- reads / writes ------------------------------------------------- #

    def get_or_insert_handle(self, key: K, factory: Callable[[Any, K], V]) -> MapHandle:
        """Materialize (the lazy pull) and return the entry handle for ``key``,
        minting it via ``factory(view, key)`` on first access and caching it.
        Returns the same handle on repeat (first-writer-wins). ``factory``
        receives the member's compute view first (``#lzcellkernel``)."""
        return self._mint_with(key, lambda view: factory(view, key))

    def get_or_insert_with(
        self, key: K, factory: Callable[[Any, K], V], ctx: Any = None
    ) -> V:
        """Get the value at ``key``, minting the entry via ``factory(view, key)``
        first if absent. For a :class:`ThreadSafeComputedMap` this is the lazy
        materialization pull — confluent across concurrent materialization
        orders. Pass the caller's compute ``ctx`` to value-thread the read of the
        entry; omit for an untracked top-level read (``#lzcellkernel``)."""
        handle = self._mint_with(key, lambda view: factory(view, key))
        return self._HANDLE.observe(self._ctx if ctx is None else ctx, handle)

    def observe(self, key: K, ctx: Any = None) -> V | None:
        """Observe ``key``'s value if the entry is present, else ``None``.
        Non-minting. The transparency law: identical whether pre-minted or minted
        on access. Pass the caller's compute ``ctx`` to value-thread the
        dependency edge inside a reactive body; omit for an untracked top-level
        read (``#lzcellkernel``)."""
        handle = self.handle(key)
        if handle is None:
            return None
        return self._HANDLE.observe(self._ctx if ctx is None else ctx, handle)

    def handle(self, key: K) -> MapHandle | None:
        """Return the existing entry handle for ``key``, or ``None``. Non-minting."""
        with self._mutex:
            return self._keyed.get(key)

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized. Non-reactive."""
        with self._mutex:
            return self._keyed.contains(key)

    def present_keys(self) -> list[K]:
        """The currently-materialized keys, in first-materialization order."""
        with self._mutex:
            return self._keyed.keys()

    def present_count(self) -> int:
        """Number of currently-materialized entries."""
        with self._mutex:
            return self._keyed.length()

    # -- Core surface: ordering, atomic move, reactive membership -------- #
    #
    # These bind every flavor. The move algebra touches no entry handle and
    # awaits nothing, so it is neither thread- nor async-coloured; the
    # membership and order signals are minted on this flavor's own graph.

    def keys(self, ctx: Any = None) -> list[K]:
        """Reactive snapshot of the keys in their current order. Subscribes the
        caller to **order** changes (add/remove **and** move/reorder), not to
        per-entry value changes. Pass the caller's compute view to value-thread
        the edge; omit for an untracked snapshot."""
        if ctx is None:
            _ = self._order_signal.value
        else:
            ctx.read(self._order_signal)
        return self.present_keys()

    def len(self, ctx: Any = None) -> int:
        """Reactive entry count. Subscribes the caller to membership changes."""
        if ctx is None:
            _ = self._membership_signal.value
        else:
            ctx.read(self._membership_signal)
        return self.present_count()

    def is_empty(self, ctx: Any = None) -> bool:
        """Reactive emptiness check."""
        return self.len(ctx) == 0

    def contains_key(self, key: K, ctx: Any = None) -> bool:
        """Reactive membership test for ``key``. Subscribes the caller to
        membership changes (add/remove of any key), not to value changes."""
        if ctx is None:
            _ = self._membership_signal.value
        else:
            ctx.read(self._membership_signal)
        return self.is_present(key)

    def len_untracked(self) -> int:
        """Non-reactive count."""
        return self.present_count()

    def position(self, key: K) -> int | None:
        """Current 0-based position of ``key`` in the order. Non-reactive."""
        with self._mutex:
            return self._keyed.position(key)

    def move_to(self, key: K, index: int) -> bool:
        """Atomically move ``key`` to ``index`` (``#lzcellmove``). The entry keeps
        the **same** handle, dependents, and lineage — that is what separates a
        reorder from a remove + re-mint. Bumps **only** the order signal."""
        with self._mutex:
            outcome = self._keyed.move_to(key, index)
        return self._apply_move(outcome)

    def move_before(self, key: K, anchor: K) -> bool:
        """Atomically move ``key`` to just before ``anchor`` (a pure reorder)."""
        with self._mutex:
            outcome = self._keyed.move_before(key, anchor)
        return self._apply_move(outcome)

    def move_after(self, key: K, anchor: K) -> bool:
        """Atomically move ``key`` to just after ``anchor`` (a pure reorder)."""
        with self._mutex:
            outcome = self._keyed.move_after(key, anchor)
        return self._apply_move(outcome)

    def remove(self, key: K) -> bool:
        """Remove ``key``'s entry and bump reactive membership. Returns whether
        the key was present.

        Matches the single-threaded map: the orphaned node is dropped, not torn
        down, because this binding's handle kinds expose no disposal hook. That
        gap is the same on all three flavors and is tracked separately — it is
        not something this flavor should solve alone and differently."""
        with self._mutex:
            _, mutation = self._keyed.remove(key)
        if not mutation.changed:
            return False
        # Off the present-set lock: the membership bump can drive a dependent
        # recompute that re-enters this map.
        self._bump_membership()
        return True

    # -- signal plumbing -------------------------------------------------- #

    def _bump_order(self) -> None:
        with self._mutex:
            self._order_version += 1
            nxt = self._order_version
        self._ts.set(self._order_signal, nxt)

    def _bump_membership(self) -> None:
        with self._mutex:
            self._version += 1
            nxt = self._version
        self._ts.set(self._membership_signal, nxt)
        self._bump_order()

    def _apply_move(self, outcome: MapMove) -> bool:
        if not outcome.applied:
            return False
        if outcome.changed:
            self._bump_order()
        return True

    @property
    def context(self) -> ThreadSafeContext:
        """The owning thread-safe context (its coalescing write boundary)."""
        return self._ts

    @property
    def entry_kind(self) -> EntryKind:
        """This map's entry kind."""
        return self._HANDLE.KIND


class ThreadSafeSourceMap[K, V](ThreadSafeReactiveMap[K, V]):
    """A thread-safe **input-cell** map: every entry is an always-materialized
    :class:`~lazily.Cell`. The ``Send + Sync`` analog of :class:`~lazily.SourceMap`.
    Adds cell-only :meth:`set`, routed through the coalescing context boundary."""

    __slots__ = ()

    _HANDLE = _SOURCE_HANDLE

    def set(self, key: K, value: V) -> None:
        """Set the value at ``key`` through the coalescing context, inserting a
        new input cell if absent. Cell-only."""
        handle = self.handle(key)
        if handle is not None:
            self._ts.set(handle, value)  # type: ignore[arg-type]
            return
        self.get_or_insert_handle(key, lambda _view, _k: value)


class ThreadSafeComputedMap[K, V](ThreadSafeReactiveMap[K, V]):
    """A thread-safe **derived-slot** map: entries are :class:`~lazily.slot` nodes
    minted lazily on access or eagerly via :meth:`materialize_all`. The
    ``Send + Sync`` analog of :class:`~lazily.ComputedMap`; a slot's value is derived,
    so it has **no ``set``**."""

    __slots__ = ()

    _HANDLE = _COMPUTED_HANDLE

    def materialize_all(
        self, keys: Iterable[K], factory: Callable[[Any, K], V]
    ) -> None:
        """**Eager materialization**: pre-mint a derived slot for every key in
        ``keys``. Observationally identical to minting each lazily on first read.
        ``factory(view, key)`` receives the member's compute view so a reactive
        read value-threads (``#lzcellkernel``)."""
        for key in keys:
            self.get_or_insert_handle(key, factory)


# ---------------------------------------------------------------------------
# Deprecated aliases (v2 kernel rename)
# ---------------------------------------------------------------------------
# See ``lazily/collection.py`` — the v2 kernel node kinds are ``Source`` and
# ``Computed``; the old map names stay as plain aliases so existing imports keep
# working. Importing them from the ``lazily`` package root emits a
# :class:`DeprecationWarning`.

#: Deprecated alias of :class:`ThreadSafeSourceMap`.
ThreadSafeCellMap = ThreadSafeSourceMap
#: Deprecated alias of :class:`ThreadSafeComputedMap`.
ThreadSafeSlotMap = ThreadSafeComputedMap
