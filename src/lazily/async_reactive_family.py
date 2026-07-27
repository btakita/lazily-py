"""Async keyed reactive collection (``#reactivemap``, async flavor) — the
async-context analog of :class:`~lazily.ReactiveMap`.

The Python counterpart of ``lazily-rs/src/async_reactive_family.rs``, the Lean
``LazilyFormal.AsyncMaterialization`` model in ``lazily-formal``, and
``lazily-spec/cell-model.md`` § "Execution-context flavors".

Keys ``K`` map to per-entry async reactive nodes: :attr:`EntryKind.SOURCE` input
cells (:class:`~lazily.Cell`, always resolved) or :attr:`EntryKind.COMPUTED` derived
slots (:class:`~lazily.AsyncSlot`, resolved **asynchronously**). Its two
specializations are :class:`AsyncSourceMap` (input cells) and :class:`AsyncComputedMap`
(derived slots).

Eager materialization is a pre-mint loop (:meth:`AsyncComputedMap.materialize_all`);
lazy is mint-on-access (:meth:`AsyncReactiveMap.get_or_insert_handle`) — there is
no eager/lazy mode flag. The transparency law here is **eventual**: a non-blocking
:meth:`AsyncReactiveMap.observe` of a derived slot returns ``None`` while pending
and the canonical value once resolved. Input cells are always resolved. Drive a
slot to resolution with :meth:`AsyncReactiveMap.resolve` (``await``). To keep the
sync/thread-safe/async maps API-parallel, the per-key factory is the same **sync**
``Callable[[K], V]``; a derived slot wraps it in a ready async recomputation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from ._keyed_order import KeyedOrder, MapMove
from .async_slot import AsyncSlot
from .cell import Cell
from .collection import EntryKind, _reads


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


__all__ = [
    "AsyncCellMap",
    "AsyncComputedMap",
    "AsyncReactiveMap",
    "AsyncSlotMap",
    "AsyncSourceMap",
]

V = TypeVar("V")

#: An async map entry's handle: an input :class:`Cell` or a derived :class:`AsyncSlot`.
type AsyncMapHandle = Cell | AsyncSlot


def _clear_dependents(handle: AsyncMapHandle) -> None:
    """Tear down a removed async-map entry.

    Source entries are ordinary graph-backed cells and therefore receive
    terminal disposal. The legacy :class:`AsyncSlot` has no dependency graph or
    terminal disposed state, so its matching teardown is a hard clear: discard
    the resolved cache and invalidate any in-flight revision.
    """
    if isinstance(handle, Cell):
        handle.dispose()
    else:
        handle.hard_clear()


class AsyncReactiveMap[K, V]:
    """The async keyed reactive collection (``#reactivemap``): keys map to
    per-entry async reactive nodes (:attr:`EntryKind.SOURCE` input cells resolved
    synchronously, or :attr:`EntryKind.COMPUTED` derived slots resolved
    asynchronously). Its two specializations are :class:`AsyncSourceMap` (input
    cells) and :class:`AsyncComputedMap` (derived slots).

    Input cells operate against the owning ``ctx`` dict (as the rest of
    ``lazily`` does); derived slots are :class:`~lazily.AsyncSlot`\\ s driven by
    ``asyncio``. See the module docs for the eventual-transparency contract.
    """

    #: The entry kind — set by the specialization.
    _KIND: EntryKind = EntryKind.SOURCE

    __slots__ = (
        "_ctx",
        "_keyed",
        "_membership_signal",
        "_order_signal",
        "_order_version",
        "_version",
    )

    def __init__(self, ctx: dict) -> None:
        self._ctx = ctx
        # Present set + key order + the move algebra, shared with the other two
        # flavors. Graph-agnostic; the reactivity below is this flavor's own.
        self._keyed: KeyedOrder[K, AsyncMapHandle] = KeyedOrder()
        # Membership and order signals minted on THIS flavor's graph. A shared
        # graph-agnostic core cannot supply reactivity.
        self._membership_signal: Cell[int] = Cell(ctx, 0)
        self._order_signal: Cell[int] = Cell(ctx, 0)
        self._version = 0
        self._order_version = 0

    @classmethod
    def new(cls, ctx: dict) -> AsyncReactiveMap[K, V]:
        """Create an empty map bound to ``ctx``."""
        return cls(ctx)

    # -- internals ------------------------------------------------------ #

    def _mint(self, key: K, factory: Callable[[Any, K], V]) -> AsyncMapHandle:
        existing = self._keyed.get(key)
        if existing is not None:
            return existing  # warm: already allocated (stable handle).
        # ``factory(view, key)`` takes a compute view first (``#lzcellkernel``).
        # The async member tracks through the async engine, not the sync compute
        # surface, so a sync-node read from the factory is untracked (cross-engine):
        # pass an untracked view over the owning dict.
        view = _reads(self._ctx)
        if self._KIND is EntryKind.SOURCE:
            # An input cell sets its value directly (always resolved).
            handle: AsyncMapHandle = Cell(self._ctx, factory(view, key))
        else:
            # A derived slot wraps the sync factory in a ready async recomputation
            # — the same node an eager pre-mint would allocate.
            async def _compute(k: K = key) -> V:
                return factory(view, k)

            handle = AsyncSlot(_compute)
        stored, mutation = self._keyed.insert(key, handle)
        if mutation.changed:
            self._bump_membership()
        handle = stored
        return handle

    # -- reads / writes ------------------------------------------------- #

    def get_or_insert_handle(
        self, key: K, factory: Callable[[Any, K], V]
    ) -> AsyncMapHandle:
        """Materialize (the lazy pull) and return the entry handle for ``key``.
        For a slot map this is the :class:`~lazily.AsyncSlot` to drive with
        :meth:`resolve` (or its own ``get_async``)."""
        return self._mint(key, factory)

    def observe(self, key: K) -> V | None:
        """Non-blocking observe: the resolved value for a cell or a resolved slot,
        or ``None`` for a slot still pending (or an absent key). The
        **eventual**-transparency law: once resolved this equals the canonical
        value. Non-minting."""
        handle = self._keyed.get(key)
        if handle is None:
            return None
        if self._KIND is EntryKind.SOURCE:
            return handle.value  # type: ignore[union-attr]
        return handle.get()

    async def resolve(self, key: K, factory: Callable[[Any, K], V]) -> V:
        """Drive ``key`` to resolution and return its canonical value, minting the
        entry via ``factory(key)`` if absent. For a cell this is immediate; for a
        slot it awaits the async recomputation."""
        handle = self._mint(key, factory)
        if self._KIND is EntryKind.SOURCE:
            return handle.value  # type: ignore[union-attr]
        return await handle.get_async()  # type: ignore[union-attr]

    def handle(self, key: K) -> AsyncMapHandle | None:
        """Return the existing entry handle for ``key``, or ``None``. Non-minting."""
        return self._keyed.get(key)

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized (present). Non-reactive."""
        return self._keyed.contains(key)

    def present_keys(self) -> list[K]:
        """The currently-materialized keys, in first-materialization order."""
        return self._keyed.keys()

    def present_count(self) -> int:
        """Number of currently-materialized entries."""
        return self._keyed.length()

    # -- Core surface: ordering, atomic move, reactive membership -------- #
    #
    # Ordering is not async-coloured: the move algebra touches no entry handle
    # and awaits nothing, so the async map carries the same Core surface as the
    # other two flavors.

    def keys(self, ctx: Any = None) -> list[K]:
        """Reactive snapshot of the keys in their current order. Subscribes the
        caller to **order** changes (add/remove **and** move/reorder), not to
        per-entry value changes."""
        if ctx is None:
            _ = self._order_signal.value
        else:
            ctx.read(self._order_signal)
        return self._keyed.keys()

    def len(self, ctx: Any = None) -> int:
        """Reactive entry count. Subscribes the caller to membership changes."""
        if ctx is None:
            _ = self._membership_signal.value
        else:
            ctx.read(self._membership_signal)
        return self._keyed.length()

    def is_empty(self, ctx: Any = None) -> bool:
        """Reactive emptiness check."""
        return self.len(ctx) == 0

    def contains_key(self, key: K, ctx: Any = None) -> bool:
        """Reactive membership test for ``key``."""
        if ctx is None:
            _ = self._membership_signal.value
        else:
            ctx.read(self._membership_signal)
        return self._keyed.contains(key)

    def len_untracked(self) -> int:
        """Non-reactive count."""
        return self._keyed.length()

    def position(self, key: K) -> int | None:
        """Current 0-based position of ``key`` in the order. Non-reactive."""
        return self._keyed.position(key)

    def move_to(self, key: K, index: int) -> bool:
        """Atomically move ``key`` to ``index`` (``#lzcellmove``). The entry keeps
        the **same** handle, dependents, and lineage. Bumps only the order
        signal, so ``keys`` readers recompute while ``len`` readers stay cached."""
        return self._apply_move(self._keyed.move_to(key, index))

    def move_before(self, key: K, anchor: K) -> bool:
        """Atomically move ``key`` to just before ``anchor`` (a pure reorder)."""
        return self._apply_move(self._keyed.move_before(key, anchor))

    def move_after(self, key: K, anchor: K) -> bool:
        """Atomically move ``key`` to just after ``anchor`` (a pure reorder)."""
        return self._apply_move(self._keyed.move_after(key, anchor))

    def remove(self, key: K) -> bool:
        """Remove and tear down ``key``'s entry, then bump reactive membership.
        Returns whether the key was present."""
        handle, mutation = self._keyed.remove(key)
        if not mutation.changed or handle is None:
            return False
        _clear_dependents(handle)
        self._bump_membership()
        return True

    # -- signal plumbing -------------------------------------------------- #

    def _bump_order(self) -> None:
        self._order_version += 1
        self._order_signal.set(self._order_version)

    def _bump_membership(self) -> None:
        self._version += 1
        self._membership_signal.set(self._version)
        self._bump_order()

    def _apply_move(self, outcome: MapMove) -> bool:
        if not outcome.applied:
            return False
        if outcome.changed:
            self._bump_order()
        return True

    @property
    def entry_kind(self) -> EntryKind:
        """This map's entry kind."""
        return self._KIND


class AsyncSourceMap[K, V](AsyncReactiveMap[K, V]):
    """An async **input-cell** map: every entry is an always-resolved
    :class:`~lazily.Cell`. The async analog of :class:`~lazily.SourceMap`. Adds
    cell-only :meth:`set`."""

    __slots__ = ()

    _KIND = EntryKind.SOURCE

    def set(self, key: K, value: V) -> None:
        """Set the value at ``key``, inserting a new input cell if absent.
        Cell-only."""
        handle = self._keyed.get(key)
        if handle is not None:
            handle.set(value)  # type: ignore[union-attr]
            return
        self._mint(key, lambda _view, _k: value)


class AsyncComputedMap[K, V](AsyncReactiveMap[K, V]):
    """An async **derived-slot** map: entries are :class:`~lazily.AsyncSlot` nodes
    resolved asynchronously, minted lazily on access or eagerly via
    :meth:`materialize_all`. The async analog of :class:`~lazily.ComputedMap`; a slot's
    value is derived, so it has **no ``set``**."""

    __slots__ = ()

    _KIND = EntryKind.COMPUTED

    def materialize_all(
        self, keys: Iterable[K], factory: Callable[[Any, K], V]
    ) -> None:
        """**Eager materialization**: pre-mint a derived slot for every key in
        ``keys``. Observationally identical to minting each lazily on first read.
        ``factory(view, key)`` takes a compute view first (``#lzcellkernel``)."""
        for key in keys:
            self._mint(key, factory)


# ---------------------------------------------------------------------------
# Deprecated aliases (v2 kernel rename)
# ---------------------------------------------------------------------------
# See ``lazily/collection.py`` — the v2 kernel node kinds are ``Source`` and
# ``Computed``; the old map names stay as plain aliases so existing imports keep
# working. Importing them from the ``lazily`` package root emits a
# :class:`DeprecationWarning`.

#: Deprecated alias of :class:`AsyncSourceMap`.
AsyncCellMap = AsyncSourceMap
#: Deprecated alias of :class:`AsyncComputedMap`.
AsyncSlotMap = AsyncComputedMap
