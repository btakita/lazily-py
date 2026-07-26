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

    __slots__ = ("_ctx", "_materialized", "_order")

    def __init__(self, ctx: dict) -> None:
        self._ctx = ctx
        self._materialized: dict[K, AsyncMapHandle] = {}
        self._order: list[K] = []

    @classmethod
    def new(cls, ctx: dict) -> AsyncReactiveMap[K, V]:
        """Create an empty map bound to ``ctx``."""
        return cls(ctx)

    # -- internals ------------------------------------------------------ #

    def _mint(self, key: K, factory: Callable[[Any, K], V]) -> AsyncMapHandle:
        existing = self._materialized.get(key)
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
        self._materialized[key] = handle
        self._order.append(key)
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
        handle = self._materialized.get(key)
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
        return self._materialized.get(key)

    def is_present(self, key: K) -> bool:
        """Whether ``key`` is currently materialized (present). Non-reactive."""
        return key in self._materialized

    def present_keys(self) -> list[K]:
        """The currently-materialized keys, in first-materialization order."""
        return list(self._order)

    def present_count(self) -> int:
        """Number of currently-materialized entries."""
        return len(self._order)

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
        handle = self._materialized.get(key)
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
